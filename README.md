# MiMO TTS Forwarder v1.0.0

> 基于 MiMo-V2.5-TTS 的 TTS 转发服务，支持声音克隆，兼容阅读 App（Legado）调用

## 功能特性

- **TTS 语音合成**：支持 9 种预置音色（中文 4 种 + 英文 4 种 + 默认）
- **声音克隆**：上传参考音频即可创建自定义音色
- **音频缓存**：相同文本 + 音色自动复用，避免重复调用
- **并发控制**：信号量限制 + 429 指数退避重试
- **API 保护**：可选 Bearer Token 认证
- **Web 管理界面**：音色管理、在线测试、API 文档，开箱即用
- **双接口兼容**：
  - Legacy API（`/api/text-to-speech`）：兼容 ms-ra-forwarder / Legado
  - OpenAI API（`/v1/audio/speech`）：兼容 OpenAI TTS 协议

## 截图

<!-- TODO: 添加管理界面截图 -->

## 快速部署

### Docker Compose（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/yourname/mimo-tts-forwarder.git
cd mimo-tts-forwarder

# 2. 复制配置文件
cp .env.example .env

# 3. 编辑 .env，填入 MIMO_API_KEY
# 注册地址：https://platform.xiaomimimo.com

# 4. 启动服务
docker compose up -d

# 5. 访问管理界面
# http://<your-ip>:8765
```

### Docker 手动构建

```bash
docker build -t mimo-tts-forwarder .
docker run -d --name mimo-tts \
  -p 8765:8765 \
  -v ./data:/app/data \
  --env-file .env \
  mimo-tts-forwarder
```

## API 文档

### Legacy TTS 接口（兼容 Legado）

**GET/POST** `/api/text-to-speech`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| text | string | (必填) | 要合成的文本 |
| voice | string | bingtang | 音色名称或别名 |
| rate | int | 0 | 语速（暂不支持） |
| volume | int | 0 | 音量（暂不支持） |
| pitch | int | 0 | 音调（暂不支持） |

示例：

```bash
# GET 请求
curl "http://localhost:8765/api/text-to-speech?voice=冰糖&text=你好世界" -o output.mp3

# POST 请求（Legado 使用此格式）
curl -X POST "http://localhost:8765/api/text-to-speech" \
  -d "text=你好世界&voice=bingtang" \
  -o output.mp3
```

### OpenAI 兼容接口

**POST** `/v1/audio/speech`

```json
{
  "model": "mimo-v2.5-tts",
  "input": "你好世界",
  "voice": "冰糖",
  "response_format": "mp3"
}
```

model 可选值：
- `mimo-v2.5-tts` - 预置音色合成
- `mimo-v2.5-tts-voiceclone` - 声音克隆
- `mimo-v2.5-tts-voicedesign` - 文字描述生成音色

### 声音克隆接口

**POST** `/v1/voices/create` (multipart/form-data)

| 参数 | 类型 | 说明 |
|------|------|------|
| audio | file | 参考音频（WAV/MP3，建议 3-10 秒） |
| name | string | 音色名称 |
| reference_text | string | 参考文本（可选，有助于提升克隆质量） |
| lang | string | 语言代码（默认 zh-CN） |

示例：

```bash
curl -X POST "http://localhost:8765/v1/voices/create" \
  -F "audio=@reference.wav" \
  -F "name=我的声音" \
  -F "reference_text=这是参考音频中说的内容"
```

### 其他接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /health | 健康检查 |
| GET | /v1/models | 列出可用模型 |
| GET | /v1/voices | 列出所有音色（预置 + 克隆） |
| GET | /v1/voices/custom | 列出自定义音色 |
| DELETE | /v1/voices/{id} | 删除音色 |
| POST | /v1/voices/test | 测试音色 |

## 预置音色列表

| 音色 ID | 名称 | 语言 | 性别 |
|---------|------|------|------|
| mimo_default | MiMo 默认 | zh-CN | 女 |
| 冰糖 | 冰糖（中文女声） | zh-CN | 女 |
| 茉莉 | 茉莉（中文女声） | zh-CN | 女 |
| 苏打 | 苏打（中文男声） | zh-CN | 男 |
| 白桦 | 白桦（中文男声） | zh-CN | 男 |
| Mia | Mia（English Female） | en-US | 女 |
| Chloe | Chloe（English Female） | en-US | 女 |
| Milo | Milo（English Male） | en-US | 男 |
| Dean | Dean（English Male） | en-US | 男 |

### 音色别名（Legado 兼容）

Legado POST 编码中文音色名可能异常，可使用以下英文别名：

| 别名 | 对应音色 |
|------|---------|
| bingtang | 冰糖 |
| moli | 茉莉 |
| suda | 苏打 |
| baihua | 白桦 |
| default | mimo_default |

## 阅读 App（Legado）配置

在阅读 App 中点击"+"添加朗读引擎，URL 填写：

**预置音色**：

```
http://<SERVER>:8765/api/text-to-speech,{"method":"POST","body":"text={{encodeURIComponent(speakText)}}&voice=bingtang"}
```

**克隆音色**（替换 `clone_xxxx` 为实际音色 ID）：

```
http://<SERVER>:8765/api/text-to-speech,{"method":"POST","body":"text={{encodeURIComponent(speakText)}}&voice=clone_xxxx"}
```

注意事项：
- 其他字段全部留空
- 语速建议设为 2.5 左右
- Content-Type 不需要填写
- 音色别名对照：bingtang=冰糖（女）、moli=茉莉（女）、suda=苏打（男）、baihua=白桦（男）、default=默认、Mia=英女、Chloe=英女、Milo=英男、Dean=英男

## 配置项说明

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| MIMO_API_KEY | (空) | MiMo API Key（必填），注册：https://platform.xiaomimimo.com |
| MIMO_BASE_URL | https://api.xiaomimimo.com/v1 | MiMo API 地址 |
| HOST | 0.0.0.0 | 监听地址 |
| PORT | 8765 | 监听端口 |
| TOKEN | (空) | API 访问令牌，设置后需 Bearer 认证 |
| MAX_AUDIO_SIZE_MB | 10 | 声音克隆上传音频最大大小（MB） |
| LOG_LEVEL | INFO | 日志级别（DEBUG/INFO/WARNING/ERROR） |

## 常见问题

**Q: 提示 API Key 未配置？**

A: 在 `.env` 文件中设置 `MIMO_API_KEY`，重启容器。注册地址：https://platform.xiaomimimo.com

**Q: Legado 中音色名乱码？**

A: 使用英文别名（bingtang/moli/suda/baihua）代替中文音色名。Legado POST 编码中文参数可能异常。

**Q: 声音克隆效果不好？**

A: 上传 3-10 秒清晰语音，尽量减少背景噪音；填写参考文本（音频中说的内容）有助于提升克隆质量。

**Q: 合成速度慢？**

A: 声音克隆比预置音色慢属于正常现象；相同文本会自动缓存，二次播放更快。

**Q: 如何更新？**

A: 拉取最新代码后 `docker compose up -d --build`，数据目录通过 volume 持久化，不会丢失。

**Q: 如何保护 API 不被滥用？**

A: 在 `.env` 中设置 `TOKEN=your_secret_token`，调用时需在请求头加 `Authorization: Bearer your_secret_token`，或在 URL 加 `?token=your_secret_token`。

## 技术栈

- Python 3.12 + FastAPI + Uvicorn
- httpx（异步 HTTP 客户端）
- aiofiles（异步文件操作）
- pydantic-settings（配置管理）
- Docker + docker-compose

## License

MIT
