from openai import OpenAI  

# 必填：从服务管控页面获取对应服务的APIKey和API Base
api_key = ""
api_base = "https://maas-api.cn-huabei-1.xf-yun.com/v2"

client = OpenAI(api_key=api_key, base_url=api_base)

def unified_chat_test(model_id, messages, use_stream=False, extra_body={}):
    """
    一个统一的函数，用于演示多种调用场景。

    :param model_id: 要调用的模型ID。
    :param messages: 对话消息列表。
    :param use_stream: 是否使用流式输出。
    :param extra_body: 包含额外请求参数的字典，如 response_format。
    """
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            stream=use_stream,
            temperature=0.7,
            max_tokens=4096,
            extra_headers={"lora_id": "0"},  # 调用微调大模型时,对应替换为模型服务卡片上的resourceId
            stream_options={"include_usage": True},
            extra_body=extra_body
        )

        if use_stream:
            # 处理流式响应
            full_response = ""
            print("--- 流式输出 ---")
            for chunk in response:
                if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    print(content, end="", flush=True)
                    full_response += content
            print("\n\n--- 完整响应 ---")
            print(full_response)
        else:
            # 处理非流式响应
            print("--- 非流式输出 ---")
            message = response.choices[0].message
            print(message.content)

    except Exception as e:
        print(f"请求出错: {e}")

if __name__ == "__main__":
    model_id = "xopqwen36v35b" # 必填：调用大模型时，对应为推理服务的模型卡片上对应的modelId

    # 1. 普通非流式调用
    print("********* 1. 普通非流式调用 *********")
    plain_messages = [{"role": "user", "content": "你好，请介绍一下自己。"}]
    unified_chat_test(model_id, plain_messages, use_stream=False)

    # 2. 普通流式调用
    print("\n********* 2. 普通流式调用 *********")
    stream_messages = [{"role": "user", "content": "写一首关于夏天的诗。"}]
    unified_chat_test(model_id, stream_messages, use_stream=True)

    # 3. JSON Mode 调用
    print("\n********* 3. JSON Mode 调用 *********")
    json_messages = [{"role": "user", "content": "请给我一个关于上海的JSON对象，包含城市名称(city)和人口数量(population)。"}]
    json_extra_body = {
        "response_format": {"type": "json_object"},
        "search_disable": True # JSON Mode下建议关闭搜索
    }
    unified_chat_test(model_id, json_messages, use_stream=False, extra_body=json_extra_body)

    # 4. 测试stop和前缀续写功能
    print("\n********* 4. 测试stop和前缀续写功能 *********")
    print("设置stop词: ['。', '！'] - 模型遇到句号或感叹号时会停止生成")
    stream_messages = [{"role": "user", "content": "给我解释下1加1等于多少。"}]
    unified_chat_test(model_id, stream_messages, use_stream=True, extra_body={"stop": ["。","！"],"continue_final_message":True})

    # 5. Tools/Function Calling 调用示例
    print("\n********* 5. Tools/Function Calling 调用示例 *********")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取指定城市的天气信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "城市名称，例如：北京、上海"
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                            "description": "温度单位"
                        }
                    },
                    "required": ["location"]
                }
            }
        }
    ]
    tool_messages = [{"role": "user", "content": "北京今天天气怎么样？"}]
    response = client.chat.completions.create(
        model=model_id,
        messages=tool_messages,
        tools=tools,
        tool_choice="auto"
    )
    message = response.choices[0].message
    if message.tool_calls:
        print(f"模型请求调用工具: {message.tool_calls[0].function.name}")
        print(f"参数: {message.tool_calls[0].function.arguments}")
    else:
        print(message.content)