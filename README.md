# VidLingua — 课程中文配音工作台

## 本地网页

网页覆盖“导入 → 人声/背景音分离 → 仅用分离人声转写 → 英文句段复核 → DeepSeek 翻译 → 中文或双语字幕 → Azure 中文配音”。面向单人讲解视频。原视频用于画面预览，分离后的背景音用于配音视频混音。

在已有 Python 3.12 虚拟环境中安装网页依赖并启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -r web-requirements.txt
.\.venv\Scripts\python.exe -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，拖入 MP4，选择是否分离人声，点击“导入并开始处理”。默认分离并保留背景音；关闭分离时用原音轨做英文识别，生成的视频会用中文配音完全替换原音轨，不保留原声。首页只显示新建任务和任务列表，点击任务进入详情。详情显示导入、分离（或跳过）、识别、整理、译前 JSON、英文校正、术语分析、翻译、配音和成片合成的进度。转写完成后会自动生成译前 JSON，经 DeepSeek 结合相邻句段校正英文，再分析课程背景和术语、翻译并配音；失败后可在详情页重试。任务与修订持久保存于 `.web_data/jobs.sqlite3`；上传视频在 `.web_data/uploads/`，处理产物仍位于 `runs/`。GPU 工作线程一次处理一个任务，同一视频会复用已完成的导入和分离产物。刷新页面可继续查看进度，失败时可重试。仅在本机监听，不包含用户登录。

句段默认显示需要复核的项目，可切换“全部”，点击时间码跳转视频并试听分离人声，或切换背景音。编辑英文并保存后，重新翻译时会自动生成译前包并对修订句段重新做词级时间对齐。对齐失败会阻止继续并提示原因。没有提醒的句段也建议按需要抽查，低对齐分数仅用于定位，不代表识词正确率。导出 JSON 保留原文、修订稿、时间范围、术语表与提醒。原混音不会进入网页版识别。

## DeepSeek 中文字幕翻译

点击右上角“设置”，在独立设置页的“DeepSeek 配置”可填写 API Key、选择 `deepseek-flash`（默认）或 `deepseek-v4-pro`，调整温度、最大输出 Token 和翻译组并发数（1–8，默认 1），并测试连接。术语分析完成后，各字幕组按这个上限并行翻译，结果仍按原顺序导出；失败时已成功的组会保留以便重试。密钥保存在当前 Windows 用户的凭据管理器，普通配置保存在 `.web_data/deepseek_config.json`；服务端也支持 `DEEPSEEK_API_KEY` 环境变量。配置接口只返回“是否已配置”，不回传密钥。服务仅应监听 `127.0.0.1`。

新任务转写完成后会自动生成译前 JSON，再向 DeepSeek 官方接口发送英文句段并结合相邻上下文做一次保守校正。校正仅修改英文正文，不改变句段 ID 和时间码；可单独下载校正英文 SRT。之后根据校正稿生成课程背景、术语表和译文。请求使用 JSON 输出，每组校验 ID、数量和顺序；失败时保留已完成的组，重试从未完成处继续。完成后可下载中文字幕、中文在上英文在下的双语字幕 `.srt`，或翻译 JSON；已完成的旧任务也可直接导出新版双语字幕，无需重新调用 DeepSeek。英文修订会使旧校正和翻译失效。中文仍建议快速复核，尤其留意页面标记的“译文存疑”和“语速偏快”。

翻译时 DeepSeek 还会为有歧义的中文多音字输出原文片段、带声调数字的拼音和同音汉字别名。网页在译文下展示这些读音，Azure 合成时将其放入 SSML 的 `phoneme` 或 `sub` 标记；手动修改中文后，旧读音标注会清除。这个环节依赖 AI 判断，重要专有名词建议试听。

## Azure 中文配音

在设置页的“Azure 中文配音”按每行一把填写 1–16 把 Speech 资源密钥，设置每把密钥最大并发数（1–8）、区域（如 `eastasia`）和中文音色。密钥保存在本机 Windows 凭据管理器，不回传到页面。保存密钥后，网页从该区域的 Azure 音色接口读取普通和 HD 中文音色；“刷新区域音色”可重新读取。新任务完成翻译后自动开始语音合成；尚未缓存的句段按密钥轮流分配，每把密钥遵守各自的并发上限。手动合成语速已移除，统一由自动基准控制。实际可用音色以当前资源返回和合成结果为准。

按翻译句段生成语音，借用句末至下句之间的空隙。普通音色先取分布于全片的样本测量时长，确定统一的 Azure 合成语速；HD 音色不支持 SSML `prosody`，因此在全部自然语音合成后确定统一的音频后处理基准。基准采用有效句段“自然时长 ÷ 时间窗”的第 75 百分位，且受上限约束；这样多数句段可以维持同一速度，单个超长句也不会把全片推得过快。设置页仅展示可调整且对所选音色有意义的基准、逐句、后处理和画面调速上限；内部普通音色合成提速限制继续自动生效。默认全片基准最多提速 8%，逐句相对基准最多再提速 5%，同时受到音频后处理总上限限制。若仍超出，就在设定上限内放慢该句到下一句之间的画面与背景音（默认最多 18%），让后续配音与字幕沿新的时间轴顺延。成片可能比原片略长。超出画面调速上限时，生成中文配音 WAV 和报告供修订台词，不截断语音，也不导出有重叠的最终视频。报告记录基准、逐句提速和画面调整。所有句段通过检查后，将中文配音与分离出的背景音混合，导出 MP4。

翻译前先从整片英文句段抽取样本和术语候选，发送给 DeepSeek 自动识别课程背景、筛选术语并给出中文译法。产物保存在 `.web_data/<任务ID>-glossary.json`，网页会展示课程背景、候选数、采用数及术语对应。背景和术语会传给每一组字幕翻译。中断后重试会复用已生成的术语表。旧版本已完成的译文不会自动改变；可在页面点击“重新分析并翻译”，旧字幕和翻译 JSON 会保存为 `previous` 文件，重新调用会消耗 API 额度。

设置页还可开启成片文字水印，设置文字、起始角落及透明度；也可选择把中文或双语字幕烧录到画面，调整字号、底部边距和描边。独立 SRT 始终保留下载。水印在生成配音 MP4 时直接烧入画面，从指定角落缓慢移动；已有成片需要重新配音才能应用新设置。

配置与接口参数参考 [DeepSeek 首次调用](https://api-docs.deepseek.com/quick_start/pricing-details-cny/)、[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)和[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。调用会使用你的 DeepSeek 账户额度。

用已提供的 demo2 运行接口验收：

```powershell
.\.venv\Scripts\python.exe smoke_web.py
```

网页版设计边界见 [WEB_UI_PLAN.md](WEB_UI_PLAN.md)。

## 命令行流程

当前实现第一步：导入视频、探测媒体流，并提取后续处理使用的音频。

## 运行

需要 Python 3、FFmpeg 和 FFprobe 在 `PATH` 中。

```powershell
py -3 import_media.py demo.mp4
```

输出目录为 `runs/<文件名>-<SHA256前12位>/`。同一文件重复运行会复用已有结果；源文件内容变化会生成新的目录。

## 输出

- `source.mp4`：导入的原始视频副本。
- `probe.json`：FFprobe 返回的完整媒体流信息。
- `manifest.json`：后续流程使用的摘要、文件哈希和产物路径。
- `source_audio.flac`：无损解码的原始声道音频，供后续混音使用。
- `asr_audio.wav`：16 kHz、单声道 PCM，供后续英语转写使用。

`manifest.json` 中的语言来自媒体流标签，尚未通过语音识别验证。导入不做转写、翻译或说话人判断。

## WhisperX 英文转写与时间对齐

项目虚拟环境使用 Python 3.12。当前安装了 WhisperX 3.8.6、PyTorch 2.8.0+cu128，并已在 RTX 3080 Ti 上验证 CUDA 可用。

重建环境时，先创建 Python 3.12 虚拟环境，安装与 Python 3.12 匹配的 CUDA 版 PyTorch wheel，再安装配套的 `torchvision==0.23.0`、`torchaudio==2.8.0` 和 `whisperx==3.8.6`。词级对齐还需要 NLTK 的 `punkt_tab` 数据；本项目将它放在 `models/nltk/tokenizers/punkt_tab/`。

```powershell
.\.venv\Scripts\python.exe transcribe_demo.py runs\demo-03c45cc6ac54
```

此命令使用 `small` 模型、Silero 语音活动检测和词级对齐；模型缓存在 `models/` 中。输出位于 `runs/demo-03c45cc6ac54/transcript/`，包含 JSON、SRT、VTT、TSV 和 TXT。首次运行需下载模型。JSON 保留逐句文本与逐词时间戳，供后续中文译配使用。

当前系统 FFmpeg 为 8.1.2，TorchCodec 会提示其内置解码不支持该版本；本次 WhisperX 转写与对齐已成功完成。以后若启用依赖 TorchCodec 解码的说话人处理，需要配置受支持的 FFmpeg 版本。

## 带背景音乐的视频

已安装 Audio Separator 0.47.0，用 BS-RoFormer 将原始立体声音轨分成 `vocals.flac` 与 `instrumental.flac`，并将人声转为 16 kHz 单声道的 `asr_vocals.wav`。分离步骤不裁剪静音，保持与原视频的时间轴一致。原混音仍保留在 `source_audio.flac`。

单命令处理并生成原混音对照字幕：

```powershell
.\.venv\Scripts\python.exe process_video.py demo2.mp4 --separate-vocals --compare-original
```

对应的分步命令：

```powershell
.\.venv\Scripts\python.exe import_media.py demo2.mp4
.\.venv\Scripts\python.exe separate_audio.py runs\demo2-33731d0c5bbd
.\.venv\Scripts\python.exe transcribe_demo.py runs\demo2-33731d0c5bbd --audio-source vocals
```

分离信息写入 `separated/metadata.json`；分离人声转写位于 `transcript_vocals/`，原混音对照位于 `transcript_original/`。未指定 `--audio-source` 时，`transcribe_demo.py` 会优先使用已完成的人声分离产物。首次分离需要下载约 639 MB 的模型，缓存在 `models/separator/`。
