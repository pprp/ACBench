import os
import json
import random
import argparse
import numpy as np
import torch

from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer
from typing import List
from vllm import LLM, SamplingParams


context_length_list = [4096]
# context_length_list = [4096, 8192, 16384]

datasets = ["niah_single_1", "niah_single_2", "niah_single_3", "niah_multikey_1", "niah_multikey_2", "niah_multikey_3",
            "niah_multiquery", "niah_multivalue", "cwe", "fwe", "vt"]

dataset2maxlen = {
    "niah_single_1": 64,
    "niah_single_2": 64,
    "niah_single_3": 64,
    "niah_multikey_1": 64,
    "niah_multikey_2": 64,
    "niah_multikey_3": 64,
    "niah_multiquery": 64,
    "niah_multivalue": 64,
    "cwe": 64,
    "fwe": 64,
    "vt": 64
}


model2maxlen = {
    "llama2": 3950,
    "llama-2": 3950,
    "llama3": 7950,
    "llama-3": 7950,
    "mistral": 31500,
    "qwen": 31500,
    "internlm": 32768
}



def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.cuda.manual_seed_all(seed)

def build_chat(prompt):
        prompt = f"[INST] {prompt} [/INST]"
        return prompt

def main(args):
    print("Loading data...")
    
    test_data = []
    prompt_list = []
    input_list = []
    outputs_list: List[List[str]] = [] # List of List
    length_list = []
    index_list = []
    
    input_max_len = 0
    model_path = args.model_path.lower()
    
    for key in model2maxlen:
        if key in model_path:
            model_max_len = model2maxlen[key]
    
    output_max_len = dataset2maxlen[args.dataset]
    
    with open(args.data_file) as fp:
        for line in fp:
            example = json.loads(line)
            length = example["length"]
            if length > input_max_len: 
                input_max_len = length

            prompt = example["input"]
            if "llama2" in args.model_path.lower():
                prompt = build_chat(prompt)
            example["prompt"] = prompt
                
            test_data.append(example)
        
    print(f"Max Length is {input_max_len}")
        
    if args.max_num_examples and len(test_data) > args.max_num_examples:
        if args.sample_method == "random":
            test_data = random.sample(test_data, args.max_num_examples)
        elif args.sample_method == "topk":
            test_data = test_data[:args.max_num_examples]
    
    for example in test_data:
        prompt_list.append(example["prompt"])
        input_list.append(example["input"])
        outputs_list.append(example["outputs"])
        length_list.append(example["length"])
        index_list.append(example["index"])

    print("Loading model...")
    model_name = model_path.split("/")[-1]

    if args.method == "FullKV":
        # Use vLLM for FullKV
        llm = LLM(
            model=args.model_path, 
            tensor_parallel_size=1, 
            gpu_memory_utilization=0.9,
            trust_remote_code=True
        )
        sampling_params = SamplingParams(
            temperature=1.0,
            max_tokens=output_max_len,
            stop=[llm.get_tokenizer().eos_token],
        )
    else:
        # Use original implementation for other methods
        if args.quant_method == "kvquant":
            from pyramidkv.quantcache import KVQuantizedCache
            from transformers import cache_utils
            cache_utils.HQQQuantizedCache = KVQuantizedCache

        tokenizer = AutoTokenizer.from_pretrained(
            args.model_path,
            use_fast=args.use_fast_tokenizer,
            padding_side="left"
        )

        from pyramidkv.monkeypatch import replace_llama,replace_mistral,replace_qwen,replace_internlm
        replace_llama(args.method.lower())
        replace_mistral(args.method.lower())
        replace_qwen(args.method.lower())
        replace_internlm(args.method.lower())
        
        model = AutoModelForCausalLM.from_pretrained(
            args.model_path,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            device_map="auto",
            use_cache=args.use_cache,
            attn_implementation=args.attn_implementation
        )
        
        tokenizer.padding_side = "left"
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
            tokenizer.pad_token_id = tokenizer.eos_token_id
        
        model.eval()

    os.makedirs(os.path.join(args.save_dir, f"{model_name}_{args.max_capacity_prompts}", str(args.context_length), args.dataset), exist_ok=True)
    fout = open(os.path.join(args.save_dir, f"{model_name}_{args.max_capacity_prompts}", str(args.context_length), args.dataset, f"{args.method}.json"), "w")
    
    for i in tqdm(range(0, len(prompt_list), args.eval_batch_size)):
        batch_prompts = prompt_list[i:i+args.eval_batch_size]
        batch_inputs = input_list[i:i+args.eval_batch_size]
        batch_answers = outputs_list[i:i+args.eval_batch_size]
        batch_lengths = length_list[i:i+args.eval_batch_size]

        if args.method == "FullKV":
            outputs = llm.generate(batch_prompts, sampling_params)
            batch_generations = [output.outputs[0].text for output in outputs]
        else:
            tokenized_prompts = tokenizer(batch_prompts, padding="longest", return_tensors="pt", add_special_tokens=True).to('cuda')
            batch_input_ids = tokenized_prompts.input_ids
            attention_mask = tokenized_prompts.attention_mask

            if len(batch_input_ids[0]) > model_max_len:
                half = int(model_max_len/2)
                prompt = tokenizer.decode(batch_input_ids[0][:half], skip_special_tokens=True)+tokenizer.decode(batch_input_ids[0][-half:], skip_special_tokens=True)
                
                tokenized_prompts = tokenizer(prompt, padding="longest", return_tensors="pt", add_special_tokens=True).to('cuda')
                batch_input_ids = tokenized_prompts.input_ids
                attention_mask = tokenized_prompts.attention_mask

            if args.max_capacity_prompts != -1:
                max_capacity_prompts = args.max_capacity_prompts
            elif args.max_capacity_prompts_ratio != -1:
                max_capacity_prompts = round(batch_input_ids.shape[1] * args.max_capacity_prompts_ratio)
            
            if args.method.lower() in ["snapkv","pyramidkv","h2o","cam", "l2norm"]:
                window_sizes = 8
            elif args.method.lower() in ["streamingllm"]:
                window_sizes = max_capacity_prompts - 4

            kernel_sizes = 7
            pooling = "maxpool"

            layers = len(model.model.layers)
            if not isinstance(window_sizes, list):
                window_sizes = [window_sizes] * layers
            if not isinstance(max_capacity_prompts, list):
                max_capacity_prompts = [max_capacity_prompts] * layers
            if not isinstance(kernel_sizes, list):
                kernel_sizes = [kernel_sizes] * layers
            for i in range(layers):
                model.model.layers[i].self_attn.config.window_size = window_sizes[i]
                model.model.layers[i].self_attn.config.max_capacity_prompt = max_capacity_prompts[i]
                model.model.layers[i].self_attn.config.kernel_size = kernel_sizes[i]
                model.model.layers[i].self_attn.config.pooling = pooling

            context_length = batch_input_ids.shape[-1]
            if args.quant_method == None:        
                output = model.generate(
                    **tokenized_prompts,
                    output_attentions = args.output_attentions,
                    max_new_tokens=output_max_len,
                    num_beams=1,
                    do_sample=False,
                    temperature=1.0,
                    min_length=context_length+1,
                    eos_token_id=[tokenizer.eos_token_id]
                )
            else:
                output = model.generate(
                    **tokenized_prompts,
                    output_attentions = args.output_attentions,
                    max_new_tokens=output_max_len,
                    num_beams=1,
                    do_sample=False,
                    temperature=1.0,
                    min_length=context_length+1,
                    eos_token_id=[tokenizer.eos_token_id],
                    cache_implementation="quantized", 
                    cache_config={"nbits": args.nbits, "backend": "HQQ","device":"cuda","residual_length":output_max_len,"axis_key":1,"q_group_size":64},
                )

            batch_outputs = tokenizer.batch_decode([output[0][context_length:]], skip_special_tokens=True)
            batch_generations = batch_outputs

            torch.cuda.empty_cache()
        
        for j in range(len(batch_prompts)):
            example = {}
            example["prompt"] = batch_prompts[j]
            example["input"] = batch_inputs[j]
            example["answers"] = batch_answers[j]
            example["pred"] = batch_generations[j]
            example["length"] = batch_lengths[j]

            fout.write(json.dumps(example) + "\n")
    

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    
    parser.add_argument("--seed", type=int, default=42, help="")
    parser.add_argument("--base_dir", type=str, default="")
    parser.add_argument("--dataset", type=str, default="")
    parser.add_argument("--data_file", type=str, default="")
    parser.add_argument("--save_dir", type=str, default="")

    parser.add_argument("--model_name", type=str, default=None, help="if specified, we will load the model to generate the predictions.")
    parser.add_argument("--model_path", type=str, default=None, help="if specified, we will load the model to generate the predictions.")
    parser.add_argument("--use_fast_tokenizer", type=bool, default=True, help="")
    parser.add_argument("--output_attentions", type=bool, default=False, help="")
    
    parser.add_argument("--max_num_examples", type=int, default=None, help="maximum number of examples to evaluate per task.")
    parser.add_argument("--sample_method", type=str, default="topk", choices=["random", "topk"], help="how to sample the examples.")
    
    parser.add_argument("--max_new_tokens", type=int, default=None, help="")
    
    parser.add_argument("--eval_batch_size", type=int, default=1, help="batch size for evaluation.")
    
    parser.add_argument("--use_cache", type=bool, default=True, help="")
    parser.add_argument("--attn_implementation", type=str,  default="flash_attention_2", choices=["flash_attention_2", "sdpa", "eager"])
    parser.add_argument("--method", type=str,  default="FullKV")
    parser.add_argument("--quant_method",type=str,default=None,choices=["kivi","kvquant"])
    parser.add_argument("--nbits", type=int, default=8, help="")
    parser.add_argument("--max_capacity_prompts", type=int, default=512, help="")
    parser.add_argument("--max_capacity_prompts_ratio", type=float, default=-1, help="")
    parser.add_argument("--steps", type=int, default=-1, help="maximum number of examples to evaluate per task.")
    
    parser.add_argument(
        "--use_chat_format", 
        action="store_true", 
        help="If given, we will use the chat format for the prompts."
    )
    parser.add_argument(
        "--chat_formatting_function", 
        type=str, 
        default="eval.templates.create_prompt_with_tulu_chat_format", 
        help="The function to use to create the chat format. This function will be dynamically imported. Please see examples in `eval/templates.py`."
    )
    
    args = parser.parse_args()
    
    set_seed(args.seed)
    
    save_dir = args.save_dir
    max_capacity_prompts = args.max_capacity_prompts
    
    for context_length in context_length_list:
        for idx, dataset in enumerate(datasets):

            print(f"Working on context length {context_length}, max_capacity_prompts: {args.max_capacity_prompts}, dataset: {dataset} - {idx}/{len(datasets)}")
            args.context_length = context_length
            args.dataset = dataset
            args.data_file = f"data/RULER/{context_length}/{args.dataset}.jsonl"

            main(args)
