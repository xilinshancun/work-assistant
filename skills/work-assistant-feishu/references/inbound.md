# 本人私聊与群内 @

先核对可信配置中的应用、owner、允许的测试群、项目和共享状态目录。启用流程需要应用开通长连接与消息事件，并由用户配置本机 Lark CLI 和 Paseo。部署选项和合成配置见[入站指南](https://github.com/xilinshancun/work-assistant/blob/main/docs/inbound.md)。

```sh
python3 -m work_assistant.inbound status --config "$WA_CONFIG"
python3 -m work_assistant.inbound inspect "$EVENT_ID" --config "$WA_CONFIG"
```

用户要求启动监听且已完成配置时，运行 `python3 -m work_assistant.inbound run --config "$WA_CONFIG"`。复用同一实例与状态目录。配置的本人私聊或本人在指定群内 @ 可触发快答、澄清或交给 Paseo 深度分析。收悉表情和最终引用回复分别以实际回执核对。入站答复不授予对外投递日报的权限。

验收时分别记录监听 ready、收到指定事件、快答或深度路由、最终答复回读的结果。成功回复一条澄清消息，只能验收该链路；实质调查还要核对问题、依据和结论。
