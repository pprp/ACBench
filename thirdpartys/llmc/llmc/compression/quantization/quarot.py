import gc
import json
import os

import torch
import torch.nn as nn
from loguru import logger

from llmc.utils.registry_factory import ALGO_REGISTRY

from .base_blockwise_quantization import BaseBlockwiseQuantization
from .hadamard_utils import apply_exact_had_to_linear, random_hadamard_matrix
from .module_utils import (_LLMC_LN_TYPES_, _TRANSFORMERS_LN_TYPES_,
                           LlmcRMSNorm, RotateLinear)


@ALGO_REGISTRY
class Quarot(BaseBlockwiseQuantization):
    def __init__(self, model, quant_config, input, padding_mask, config, modality='language'):
        super().__init__(model, quant_config, input, padding_mask, config, modality)
        self.dev = torch.device('cuda')
        self.add_quant_config()
        self.preprocess()

    def preprocess(self):
        if torch.equal(
            self.model.get_head_layers()[0].weight,
            self.model.get_embed_layers()[0].weight,
        ):
            logger.info('Tie weight! Copy embed_layer for head_layer!')
            del self.model.get_head_layers()[0].weight
            w = self.model.get_embed_layers()[0].weight.clone()
            self.model.get_head_layers()[0].weight = nn.Parameter(w)

        self.remove_mean_from_embed()

        self.Q = self.get_orthogonal_matrix()
        self.rotate_embeddings(self.Q)

        pre_head_ln = self.model.get_pre_head_layernorm_layers()[0]
        self.fuse_ln_fcs(pre_head_ln, self.model.get_head_layers())

        self.model.replace_module_subset(
            LlmcRMSNorm,
            self.model.model,
            {'layers': {'model.norm': pre_head_ln}},
            None,
            {},
        )

        self.rotate_head(self.Q)

        # for vlm model
        if self.model.vlm_model is not None:
            logger.info('For vlm model, quarot need rotate last layer in vision_projector.')
            """
            txt_input     img_input
                |             |
            Embeding      vision_projector
                |             |
                       |
                  input_embeds
                       |
                       Y
            Therefore:
            X_txt ~ W_embeding * Q = X_txt ~ (W_embeding * Q)
            X_proj * W_proj.t() * Q = X_proj * (Q.t() * W_proj).t()
            """
            assert hasattr(self.model, 'vision_projector')
            dtype = self.model.vision_projector[-1].weight.dtype
            device = self.Q.device
            W = self.model.vision_projector[-1].weight.data.to(device=device, dtype=torch.float64)
            self.model.vision_projector[-1].weight.data = torch.matmul(self.Q.T, W).to(device='cpu', dtype=dtype) # noqa

        gc.collect()
        torch.cuda.empty_cache()

    @torch.no_grad()
    def add_quant_config(self):
        self.rotate_mode = self.quant_config['special']['rotate_mode']

    def get_orthogonal_matrix(self):
        if self.rotate_mode == 'random':
            try:
                return random_orthogonal_matrix(self.hidden_size, self.dev)
            except NameError:
                raise RuntimeError(
                    'Function random_orthogonal_matrix is not defined.'
                )
        elif self.rotate_mode == 'hadamard':
            return random_hadamard_matrix(self.hidden_size, self.dev)
        else:
            raise ValueError(f'Unsupported mode {self.mode}')

    def block_transform(self, block):
        logger.info(f'Start transform the {self.block_idx+1}-th block')

        if self.online_rotate:
            self.replace_rotate_linears(block)
        subsets = self.model.get_subsets_in_block(block)
        for index, subset in enumerate(subsets):
            self.subset_transform(block, subset)

        self.model.replace_module_block(LlmcRMSNorm, block, self.block_idx, {})

        logger.info(f'block:{block}')
        logger.info(f'End transform the {self.block_idx+1}-th block')

    @torch.no_grad()
    def subset_transform(self, block, subset):
        prev_op = subset['prev_op']
        layers_dict = subset['layers']
        assert (
            len(prev_op) == 1
        ), 'Only support single prev_op. If multi prev_ops, code need to be updated.'

        layers = list(layers_dict.values())

        if 'skip_rotate' in subset and subset['skip_rotate']:
            return

        if 'need_rotate_alone' in subset and subset['need_rotate_alone']:
            assert isinstance(prev_op[0], tuple(_LLMC_LN_TYPES_ + _TRANSFORMERS_LN_TYPES_))
            pre_layers = subset['pre_layers']
            post_layers = subset['post_layers']
            for layer in pre_layers + post_layers:
                layer = layer.cuda()
            self.fuse_ln_fcs(prev_op[0], layers)
            self.rotate_pre_layers(pre_layers, self.Q)
            self.rotate_post_layers(post_layers, self.Q)

        if isinstance(prev_op[0], tuple(_LLMC_LN_TYPES_ + _TRANSFORMERS_LN_TYPES_)):
            self.fuse_ln_fcs(prev_op[0], layers)
            self.rotate_pre_layers(layers, self.Q)
        else:
            if self.config['model']['type'] in ['Opt', 'StableLm']:
                self.bake_mean_into_fc(layers[0])

            if 'is_mlp' in subset and subset['is_mlp']:
                self.rotate_post_layers(
                    layers, self.Q, exact_had=True if self.online_rotate else False
                )
            else:
                self.rotate_post_layers(layers, self.Q, exact_had=False)
                if self.online_rotate:
                    if prev_op[0] is not None:
                        apply_exact_had_to_linear(
                            prev_op[0], had_dim=self.head_dim, output=True
                        )
                        apply_exact_had_to_linear(layers[0], had_dim=-1, output=False)

    @torch.no_grad()
    def save_model(self, path):
        super().save_model(path)
        path = os.path.join(path, 'config.json')
        with open(path, 'r') as f:
            config = json.load(f)
        if 'tie_word_embeddings' in config:
            config['tie_word_embeddings'] = False
        with open(path, 'w') as f:
            json.dump(config, f, indent=4)
