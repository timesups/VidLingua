# 项目结构

项目按处理阶段、外部服务、任务编排和持久化划分。这个边界参考了 [VideoLingo 的模块划分](https://docs.videolingo.io/en-US/docs/tech) 与 [pyVideoTrans 的流程架构](https://github.com/jianchang512/pyvideotrans/blob/main/docs/architecture.md)，并以现有本地工作台的功能为准。

```text
VidLingua/
├─ vidlingua/
│  ├─ api/          # HTTP 路由、请求模型、配置接口
│  ├─ pipeline/     # 导入、分离、转写、译前整理、命令行串联
│  ├─ providers/    # DeepSeek 翻译和 Azure Speech 接入
│  ├─ services/     # 翻译、配音的后台任务编排
│  ├─ rendering/    # 字幕、水印等视频叠加层
│  └─ storage/      # SQLite 任务及句段读写
├─ web/             # 浏览器界面；js/ 内按基础、任务、设置拆分
├─ tests/           # 回归测试
├─ scripts/         # 手动验收脚本
├─ samples/         # 示例素材
└─ docs/            # 设计与架构说明
```

主流程：`api` 接收任务 → `pipeline` 导入并识别 → `services.translation` 调用 `providers.deepseek` → `services.dubbing` 调用 `providers.azure_speech` 并合成视频。`storage` 保存任务和句段状态；`rendering` 提供字幕与水印滤镜。

运行产物继续放在根目录的 `.web_data/`、`runs/`、`models/`，现有数据库中的绝对文件路径不变。根目录 `web_app.py` 及 `vidlingua/` 下的旧 CLI 模块是兼容入口；新增代码应直接导入对应子包。

启动：`python -m uvicorn web_app:app --host 127.0.0.1 --port 8001`。测试：`python -m unittest discover -s tests -p "test_*.py"`。
