meta_template_dict = dict(
    internlm = [
        dict(role='system', begin='<|System|>:', end='\n'),
        dict(role='user', begin='<|User|>:', end='\n'),
        dict(role='function', begin='<|System|>:', end='\n'),
        dict(
            role='assistant',
            begin='<|Bot|>:',
            end='<eoa>\n',
            generate=True)
    ],
    llama2 = [
        dict(role='system', begin='[INST]', end='[\INST]'),
        dict(role='user', begin='[INST]', end='[\INST]'),
        dict(role='function', begin='[INST]', end='[\INST]'),
        dict(role='assistant',
                begin='',
                end='</s>',
                generate=True),
    ],
    qwen = [
        dict(role='user', begin='<|im_start|>user\n', end='<|im_end|>'),
        dict(role='assistant', begin='<|im_start|>assistant\n', end='<|im_end|>', generate=True),
        dict(role='function', begin='<|im_start|>function\n', end='<|im_end|>'),
        dict(role='system', begin='<|im_start|>system\n', end='<|im_end|>'),
    ],
    vicuna = [
        dict(role='user', begin='user: ', end='\n'),
        dict(role='system', begin='user: ', end='\n'),
        dict(role='function', begin='user: ', end='\n'),
        dict(role='assistant',
                begin='assistant: ',
                end='\n',
                generate=True),
    ],
    # we use chat APITemplateParser for chatglm due to some specific designs from ChatGLM
    chatglm = [
        dict(role='system', api_role='user'),
        dict(role='user', api_role='user'),
        dict(role='function', api_role='user'),
        dict(role='assistant',
            api_role='assistant',
            generate=True)
    ],
    baichuan = [
        dict(role='user', begin='<reserved_106>', end='\n'),
        dict(role='system', begin='<reserved_106>', end='\n'),
        dict(role='function', begin='<reserved_106>', end='\n'),
        dict(role='assistant', begin='<reserved_107>', end='\n',
                generate=True),
    ],
    mistral = [
        dict(role='system', begin='[INST] ', end=' [/INST]'),
        dict(role='user', begin='[INST] ', end=' [/INST]'),
        dict(role='assistant', begin='', end='\n', generate=True),
        dict(role='function', begin='', end='\n'),
    ]
)