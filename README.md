# VidLingua — 课程中文配音工作台

## 本地网页

网页覆盖“导入 → 人声/背景音分离 → 仅用分离人声转写 → 英文句段复核 → DeepSeek 翻译 → 中文或双语字幕 → Azure 中文配音”。面向单人讲解视频。原视频用于画面预览，分离后的背景音用于配音视频混音。

在已有 Python 3.12 虚拟环境中安装网页依赖并启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn web_app:app --host 127.0.0.1 --port 8001
```

也可双击 `start.bat`；默认使用 8001 端口，传入端口号可覆盖默认值。

打开 [http://127.0.0.1:8001](http://127.0.0.1:8001)，在新建任务中选择“本地 MP4”或“YouTube 链接”。YouTube 支持单个普通视频、Shorts 和直播回放链接；服务用 yt-dlp 下载为本地 MP4、检查画面与音轨，再接入现有处理流程。下载进度和失败原因显示在任务详情页，可重试；下载依照系统代理配置。两种来源都可选择是否分离人声。默认分离并保留背景音；关闭分离时用原音轨做英文识别，生成的视频会用中文配音完全替换原音轨，不保留原声。首页只显示新建任务和任务列表，点击任务进入详情。详情显示下载（YouTube 任务）、导入、分离（或跳过）、识别、整理、译前 JSON、英文校正、术语分析、翻译、配音和成片合成的整体进度与当前阶段进度；下载、分离、转写、英文校正、翻译和语音合成阶段会展示内部计数。页面底部的日志窗口可查看最近的处理输出与阶段切换，运行中自动刷新。转写完成后会自动生成译前 JSON，经 DeepSeek 结合相邻句段校正英文，再分析课程背景和术语、翻译并配音；失败后可在详情页重试。任务与修订持久保存于 `.web_data/jobs.sqlite3`；上传或下载的视频在 `.web_data/uploads/`，处理产物仍位于 `runs/`。GPU 工作线程一次处理一个任务，同一视频会复用已完成的导入和分离产物。刷新页面可继续查看进度，失败时可重试。仅在本机监听，不包含用户登录。

任务列表以表格展示，可按任务名、文件名或 ID 搜索并按状态筛选；勾选任务后可批量重跑或删除。任务详情也提供单项“重跑任务”和“删除任务”。重跑会清除该任务的旧句段、翻译和成片，从译前整理重新执行自动流程，复用已经下载的 YouTube 视频、已完成的转写和匹配的语音缓存。删除会清理任务记录、上传视频、成片、日志和该任务的配音缓存；可能由多个任务共用的 `runs/` 中间目录会保留。运行中的任务不能重跑或删除。

设置页的“YouTube 下载与代理”可选择跟随系统代理、直连或本机 HTTP 代理端口；保存后对新的下载立即生效。需要登录的视频可以导入 Netscape 格式的 `cookies.txt`，保存在被 Git 忽略的 `.web_data/youtube_cookies.txt`，页面只显示是否已保存，不回传 Cookie 内容，也可一键移除。连接测试只读取公开视频元数据，不下载视频。代理设置保存在 `.web_data/youtube_config.json`。

句段默认显示需要复核的项目，可切换“全部”，点击时间码跳转视频并试听分离人声，或切换背景音。编辑英文并保存后，重新翻译时会自动生成译前包并对修订句段重新做词级时间对齐。对齐失败会阻止继续并提示原因。没有提醒的句段也建议按需要抽查，低对齐分数仅用于定位，不代表识词正确率。导出 JSON 保留原文、修订稿、时间范围、术语表与提醒。原混音不会进入网页版识别。

译前整理使用 WhisperX 词级时间戳控制单段最长 9 秒、110 个英文字符。超长句优先在句末或分句标点处拆开；没有合适标点时优先选择从句起点、明显停顿，最后才使用普通词间边界，并避免把冠词、介词等留在段尾。此规则只影响之后生成的译前句段，已有任务的翻译和配音不会自动改写。

## DeepSeek 中文字幕翻译

点击右上角“设置”，在独立设置页的“DeepSeek 配置”可填写 API Key、选择 `deepseek-flash`（默认）或 `deepseek-v4-pro`，调整温度、最大输出 Token 和翻译组并发数（1–8，默认 1），并测试连接。术语分析完成后，各字幕组按这个上限并行翻译，结果仍按原顺序导出；失败时已成功的组会保留以便重试。密钥保存在当前 Windows 用户的凭据管理器，普通配置保存在 `.web_data/deepseek_config.json`；服务端也支持 `DEEPSEEK_API_KEY` 环境变量。配置接口只返回“是否已配置”，不回传密钥。服务仅应监听 `127.0.0.1`。

新任务转写完成后会自动生成译前 JSON，再向 DeepSeek 官方接口发送英文句段并结合相邻上下文做一次保守校正。校正仅修改英文正文，不改变句段 ID 和时间码；可单独下载校正英文 SRT。之后根据校正稿生成课程背景、术语表和译文。请求使用 JSON 输出，每组校验 ID、数量和顺序；失败时保留已完成的组，重试从未完成处继续。完成后可下载中文字幕、中文在上英文在下的双语字幕 `.srt`，或翻译 JSON；已完成的旧任务也可直接导出新版双语字幕，无需重新调用 DeepSeek。英文修订会使旧校正和翻译失效。中文仍建议快速复核，尤其留意页面标记的“译文存疑”和“语速偏快”。

翻译时 DeepSeek 还会为有歧义的中文多音字输出原文片段、带声调数字的拼音和同音汉字别名。网页在译文下展示这些读音，Azure 合成时将其放入 SSML 的 `phoneme` 或 `sub` 标记；手动修改中文后，旧读音标注会清除。这个环节依赖 AI 判断，重要专有名词建议试听。

## Azure 中文配音

在设置页的“Azure 语音模型”按每行一把填写 1–16 把 Speech 资源密钥，设置每把密钥最大并发数（1–8）、区域（如 `eastasia`）和中文音色。密钥保存在本机 Windows 凭据管理器，不回传到页面。保存密钥后，可点击“检测已保存密钥”逐把访问所填区域的 Azure 音色接口，查看连接结果及当前音色是否返回；结果只显示密钥序号，不回传密钥。网页也可从该区域读取普通和 HD 中文音色；“刷新区域音色”可重新读取。HD 音色可设置“表现温度”，默认 0.4；它使用 Azure 的 `temperature` 参数减少语气波动，不改变音色名称。当前 `Yunxiao DragonHDFlash` 未列出 `calm` 风格，因此不发送不受支持的风格标签；修改表现温度后重新配音会生成新的缓存片段，旧成片保持不变。相关支持范围见 [Azure HD 音色文档](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/high-definition-voices)。新任务完成翻译后自动开始语音合成；尚未缓存的句段按密钥轮流分配，每把密钥遵守各自的并发上限。网络断连、超时、HTTP 408/429 和常见服务端错误会自动退避重试，最多尝试 5 次；失败批次中已经合成成功的句段会保留在缓存，任务重试只请求缺失片段。手动合成语速已移除，统一由自动基准控制。实际可用音色以当前资源返回和合成结果为准。

按翻译句段生成语音，借用句末至下句之间的空隙。普通音色先取分布于全片的样本测量时长，确定统一的 Azure 合成语速；HD 音色不支持 SSML `prosody`，因此在全部自然语音合成后确定统一的音频后处理基准。基准采用有效句段“自然时长 ÷ 时间窗”的第 75 百分位，且受上限约束；这样多数句段可以维持同一速度，单个超长句也不会把全片推得过快。设置页将 Azure 与豆包的语音模型分栏配置；共享的“速度与时间轴”分栏单独保存基准、逐句、后处理和画面调速上限，两种引擎均使用这组参数；内部普通音色合成提速限制继续自动生效。默认全片基准最多提速 8%，逐句相对基准最多再提速 5%，同时受到音频后处理总上限限制；四项可选上限现为基准 30%、逐句 25%、音频后处理 75%、画面调速 50%，默认配置不变。若配音仍长于这一句到下一句的画面，就在设定上限内放慢该段（默认最多 18%），后续配音与字幕顺延。若配音短于这段画面，则按同样上限加快，后续时间轴提前。成片可能比原片略长或略短。超出画面调速上限时，生成中文配音 WAV 和报告供修订台词，不截断语音，也不导出有重叠的最终视频。报告记录基准、逐句提速和画面调整。所有句段通过检查后，将中文配音与分离出的背景音混合，导出 MP4。

成片合成需要重新编码画面时（画面调速、烧录字幕或水印），默认使用 FFmpeg 的 NVIDIA `h264_nvenc`；若该编码器在当前机器上失败，会自动改用 `libx264` 重试。仅替换音轨且画面无变化时直接复制原视频流。字幕、水印和画面调速滤镜仍由 CPU 处理，因此 GPU 加速主要作用于视频编码。任务日志和配音报告的 `video_encoder` 字段可查看实际使用的编码器。

时长待复核时，任务详情中的每条问题句会并排显示中文字幕与英文原文，提供该句实际后处理速度的中文配音试听、原片定位及就地修改台词。保存中文仅更新当前句，不刷新整页，便于连续编辑。中文留空表示跳过该句的配音和成片字幕；英文原句仍保留在翻译 JSON 中，之后可恢复编辑。保存后旧配音轨会失效，但上次复核清单与旧句音频仍可试听。全部修改完成后点击“重新配音”，新报告会重新计算时长，未修改的语音片段会复用缓存。成片完成后，任务详情顶部会显示可拖动进度的成片预览播放器，并保留原片和分离音轨试听。

翻译前先从整片英文句段抽取样本和术语候选，发送给 DeepSeek 自动识别课程背景、筛选术语并给出中文译法。产物保存在 `.web_data/<任务ID>-glossary.json`，网页会展示课程背景、候选数、采用数及术语对应。背景和术语会传给每一组字幕翻译。中断后重试会复用已生成的术语表。旧版本已完成的译文不会自动改变；可在页面点击“重新分析并翻译”，旧字幕和翻译 JSON 会保存为 `previous` 文件，重新调用会消耗 API 额度。

设置页还可开启成片文字水印，设置文字、起始角落及透明度；也可选择把中文或双语字幕烧录到画面，调整字号、底部边距和描边。独立 SRT 始终保留下载。水印在生成配音 MP4 时直接烧入画面，从指定角落缓慢移动；已有成片需要重新配音才能应用新设置。

配置与接口参数参考 [DeepSeek 首次调用](https://api-docs.deepseek.com/quick_start/pricing-details-cny/)、[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)和[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。调用会使用你的 DeepSeek 账户额度。

用已提供的 demo2 运行接口验收：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_web.py
```

网页版设计边界见 [WEB_UI_PLAN.md](docs/WEB_UI_PLAN.md)。

## 项目目录

- `vidlingua/api/`：FastAPI 路由与设置接口；`vidlingua/services/`：翻译、配音后台任务；`vidlingua/storage/`：SQLite 任务与句段持久化。
- `vidlingua/pipeline/`：视频导入、人声分离、WhisperX 转写和译前整理；`vidlingua/providers/`：DeepSeek、Azure 接口与音频处理；`vidlingua/rendering/`：字幕和水印渲染。
- `web/`：页面的 HTML、CSS 和分为基础函数、任务操作、设置界面的 JavaScript；根目录 `web_app.py` 与 `vidlingua/` 下的旧命令模块仅作为兼容入口，新代码直接引用上述子包。
- `tests/`：单元与接口回归测试；`scripts/`：手动验收脚本；`samples/`：随项目提供的 demo 视频；`docs/`：设计说明。
- `.web_data/`：现有任务数据库、上传视频和成片；`runs/`：已导入视频的处理中间产物；`models/`：模型缓存。三处沿用原路径，不迁移已有任务。

运行回归测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

## 命令行流程

当前实现第一步：导入视频、探测媒体流，并提取后续处理使用的音频。

## 运行

需要 Python 3、FFmpeg 和 FFprobe 在 `PATH` 中。

```powershell
py -3 -m vidlingua.import_media samples\demo.mp4
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
.\.venv\Scripts\python.exe -m vidlingua.transcribe_demo runs\demo-03c45cc6ac54
```

此命令使用 `small` 模型、Silero 语音活动检测和词级对齐；模型缓存在 `models/` 中。输出位于 `runs/demo-03c45cc6ac54/transcript/`，包含 JSON、SRT、VTT、TSV 和 TXT。首次运行需下载模型。JSON 保留逐句文本与逐词时间戳，供后续中文译配使用。

当前系统 FFmpeg 为 8.1.2，TorchCodec 会提示其内置解码不支持该版本；本次 WhisperX 转写与对齐已成功完成。以后若启用依赖 TorchCodec 解码的说话人处理，需要配置受支持的 FFmpeg 版本。

## 带背景音乐的视频

已安装 Audio Separator 0.47.0，用 BS-RoFormer 将原始立体声音轨分成 `vocals.flac` 与 `instrumental.flac`，并将人声转为 16 kHz 单声道的 `asr_vocals.wav`。分离步骤不裁剪静音，保持与原视频的时间轴一致。原混音仍保留在 `source_audio.flac`。

单命令处理并生成原混音对照字幕：

```powershell
.\.venv\Scripts\python.exe -m vidlingua.process_video samples\demo2.mp4 --separate-vocals --compare-original
```

对应的分步命令：

```powershell
.\.venv\Scripts\python.exe -m vidlingua.import_media samples\demo2.mp4
.\.venv\Scripts\python.exe -m vidlingua.separate_audio runs\demo2-33731d0c5bbd
.\.venv\Scripts\python.exe -m vidlingua.transcribe_demo runs\demo2-33731d0c5bbd --audio-source vocals
```

分离信息写入 `separated/metadata.json`；分离人声转写位于 `transcript_vocals/`，原混音对照位于 `transcript_original/`。未指定 `--audio-source` 时，`transcribe_demo.py` 会优先使用已完成的人声分离产物。首次分离需要下载约 639 MB 的模型，缓存在 `models/separator/`。
