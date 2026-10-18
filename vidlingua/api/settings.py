"""Configuration endpoints for rendering, translation, and speech providers."""

from __future__ import annotations


def watermark_config(context, ):
    WATERMARK_CONFIG = context.WATERMARK_CONFIG
    load_watermark_config = context.load_watermark_config
    return load_watermark_config(WATERMARK_CONFIG)



def configure_watermark(context, edit: WatermarkConfigEdit):
    HTTPException = context.HTTPException
    WATERMARK_CONFIG = context.WATERMARK_CONFIG
    save_watermark_config = context.save_watermark_config
    try:
        return save_watermark_config(WATERMARK_CONFIG, edit.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc



def subtitle_config(context, ):
    SUBTITLE_CONFIG = context.SUBTITLE_CONFIG
    load_subtitle_config = context.load_subtitle_config
    return load_subtitle_config(SUBTITLE_CONFIG)



def configure_subtitles(context, edit: SubtitleConfigEdit):
    HTTPException = context.HTTPException
    SUBTITLE_CONFIG = context.SUBTITLE_CONFIG
    save_subtitle_config = context.save_subtitle_config
    try:
        return save_subtitle_config(SUBTITLE_CONFIG, edit.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc



def deepseek_config(context, ):
    DEEPSEEK_CONFIG = context.DEEPSEEK_CONFIG
    MODELS = context.MODELS
    get_api_key = context.get_api_key
    load_config = context.load_config
    validate_api_key = context.validate_api_key
    key = get_api_key()
    try:
        if key:
            validate_api_key(key)
        usable = bool(key)
    except ValueError:
        usable = False
    return {**load_config(DEEPSEEK_CONFIG), "api_key_configured": bool(key),
            "api_key_usable": usable,
            "base_url": "https://api.deepseek.com", "models": sorted(MODELS)}



def configure_deepseek(context, edit: DeepSeekConfigEdit):
    DEEPSEEK_CONFIG = context.DEEPSEEK_CONFIG
    DEFAULTS = context.DEFAULTS
    HTTPException = context.HTTPException
    deepseek_config = context.deepseek_config
    delete_api_key = context.delete_api_key
    save_config = context.save_config
    set_api_key = context.set_api_key
    validate_api_key = context.validate_api_key
    config = edit.model_dump(include=set(DEFAULTS))
    try:
        if edit.api_key:
            validate_api_key(edit.api_key)
        save_config(DEEPSEEK_CONFIG, config)
        if edit.api_key:
            set_api_key(edit.api_key)
        elif edit.remove_key:
            delete_api_key()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return deepseek_config()



def test_deepseek(context, ):
    DEEPSEEK_CONFIG = context.DEEPSEEK_CONFIG
    HTTPException = context.HTTPException
    call_deepseek = context.call_deepseek
    get_api_key = context.get_api_key
    load_config = context.load_config
    validate_api_key = context.validate_api_key
    key = get_api_key()
    if not key:
        raise HTTPException(409, "请先配置 API Key")
    try:
        validate_api_key(key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        result = call_deepseek(key, load_config(DEEPSEEK_CONFIG), [
            {"role": "system", "content": "Return JSON only, exactly {\"ok\":true}."},
            {"role": "user", "content": "Connection test. Return JSON."},
        ])
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    content = result["content"]
    return {"ok": isinstance(content, dict) and content.get("ok") is True,
            "model": load_config(DEEPSEEK_CONFIG)["model"]}



def azure_config(context, ):
    AZURE_CONFIG = context.AZURE_CONFIG
    AZURE_VOICES = context.AZURE_VOICES
    get_azure_keys = context.get_azure_keys
    load_azure_config = context.load_azure_config
    keys = get_azure_keys()
    visible_config = load_azure_config(AZURE_CONFIG)
    visible_config.pop("max_synthesis_adjust", None)
    return {**visible_config, "api_key_configured": bool(keys),
            "key_count": len(keys),
            "voices": AZURE_VOICES}



def configure_azure(context, edit: AzureConfigEdit):
    AZURE_CONFIG = context.AZURE_CONFIG
    HTTPException = context.HTTPException
    azure_config = context.azure_config
    delete_azure_key = context.delete_azure_key
    load_azure_config = context.load_azure_config
    save_azure_config = context.save_azure_config
    set_azure_keys = context.set_azure_keys
    validate_azure_config = context.validate_azure_config
    validate_azure_key = context.validate_azure_key
    config = {**load_azure_config(AZURE_CONFIG),
              **edit.model_dump(exclude={"api_keys", "api_key", "remove_key"})}
    try:
        config = validate_azure_config(config)
        new_keys = edit.api_keys or edit.api_key
        if new_keys:
            parsed = [line.strip() for line in new_keys.splitlines() if line.strip()]
            for item in parsed:
                validate_azure_key(item)
            if not 1 <= len(set(parsed)) <= 16:
                raise ValueError("请填写 1–16 把 Azure Speech 密钥")
        save_azure_config(AZURE_CONFIG, config)
        if new_keys:
            set_azure_keys(parsed)
        elif edit.remove_key:
            delete_azure_key()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return azure_config()



def test_azure(context, ):
    AZURE_CONFIG = context.AZURE_CONFIG
    HTTPException = context.HTTPException
    azure_chinese_voices = context.azure_chinese_voices
    azure_voice_list = context.azure_voice_list
    get_azure_key = context.get_azure_key
    load_azure_config = context.load_azure_config
    key = get_azure_key()
    config = load_azure_config(AZURE_CONFIG)
    if not key or not config["region"]:
        raise HTTPException(409, "请先填写 Azure Speech 密钥和区域")
    try:
        voices = azure_voice_list(key, config["region"])
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    chinese = azure_chinese_voices(voices)
    return {"ok": any(voice["name"] == config["voice"] for voice in chinese),
            "voice": config["voice"], "voice_count": len(chinese), "voices": chinese,
            "region": config["region"]}



def doubao_config(context, ):
    DOUBAO_CONFIG = context.DOUBAO_CONFIG
    DOUBAO_RESOURCE_IDS = context.DOUBAO_RESOURCE_IDS
    DOUBAO_VOICES = context.DOUBAO_VOICES
    get_doubao_key = context.get_doubao_key
    load_doubao_config = context.load_doubao_config
    validate_doubao_key = context.validate_doubao_key
    key = get_doubao_key()
    try:
        usable = bool(key and validate_doubao_key(key))
    except ValueError:
        usable = False
    return {**load_doubao_config(DOUBAO_CONFIG), "api_key_configured": bool(key),
            "api_key_usable": usable, "resources": list(DOUBAO_RESOURCE_IDS),
            "voices": [{"name": name, "local_name": label} for name, label in DOUBAO_VOICES.items()]}


def configure_doubao(context, edit: DoubaoConfigEdit):
    DOUBAO_CONFIG = context.DOUBAO_CONFIG
    DOUBAO_DEFAULTS = context.DOUBAO_DEFAULTS
    HTTPException = context.HTTPException
    delete_doubao_key = context.delete_doubao_key
    doubao_config = context.doubao_config
    load_doubao_config = context.load_doubao_config
    save_doubao_config = context.save_doubao_config
    set_doubao_key = context.set_doubao_key
    validate_doubao_config = context.validate_doubao_config
    validate_doubao_key = context.validate_doubao_key
    config = {**load_doubao_config(DOUBAO_CONFIG), **edit.model_dump(include=set(DOUBAO_DEFAULTS))}
    try:
        config = validate_doubao_config(config)
        if edit.api_key:
            validate_doubao_key(edit.api_key)
        save_doubao_config(DOUBAO_CONFIG, config)
        if edit.api_key:
            set_doubao_key(edit.api_key)
        elif edit.remove_key:
            delete_doubao_key()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return doubao_config()


def test_doubao(context, ):
    DOUBAO_CONFIG = context.DOUBAO_CONFIG
    HTTPException = context.HTTPException
    doubao_synthesize = context.doubao_synthesize
    get_doubao_key = context.get_doubao_key
    load_doubao_config = context.load_doubao_config
    validate_doubao_config = context.validate_doubao_config
    validate_doubao_key = context.validate_doubao_key
    key = get_doubao_key()
    if not key:
        raise HTTPException(409, "请先配置豆包语音 API Key")
    try:
        validate_doubao_key(key)
        config = validate_doubao_config(load_doubao_config(DOUBAO_CONFIG))
        audio = doubao_synthesize(key, config, "测试。", 1.0)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": len(audio) > 44, "speaker": config["speaker"], "resource_id": config["resource_id"]}


def check_azure_keys(context, request: AzureKeysCheck):
    AZURE_CONFIG = context.AZURE_CONFIG
    HTTPException = context.HTTPException
    ThreadPoolExecutor = context.ThreadPoolExecutor
    azure_voice_list = context.azure_voice_list
    get_azure_keys = context.get_azure_keys
    load_azure_config = context.load_azure_config
    re = context.re
    keys = get_azure_keys()
    config = load_azure_config(AZURE_CONFIG)
    region = (request.region or config["region"]).strip().lower()
    if not keys:
        raise HTTPException(409, "请先保存 Azure Speech 密钥")
    if not re.fullmatch(r"[a-z0-9-]{2,40}", region):
        raise HTTPException(400, "Azure 区域格式不正确")

    def check(index: int, key: str) -> dict:
        try:
            voices = azure_voice_list(key, region)
            available = any(item.get("ShortName") == config["voice"] for item in voices)
            return {"index": index, "ok": True, "voice_available": available,
                    "message": "密钥有效；当前音色可用" if available else "密钥有效；当前音色未在该区域返回"}
        except Exception as exc:
            return {"index": index, "ok": False, "voice_available": False,
                    "message": f"检测失败：{exc}"}

    with ThreadPoolExecutor(max_workers=min(4, len(keys))) as pool:
        results = list(pool.map(lambda entry: check(*entry), enumerate(keys, 1)))
    return {"region": region, "total": len(keys), "valid": sum(item["ok"] for item in results),
            "results": results}

