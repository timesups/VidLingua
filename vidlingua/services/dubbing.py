"""Dubbing background processing, independent of HTTP routes."""

from __future__ import annotations


def dub_job(context, job_id: str):
    AZURE_CONFIG = context.AZURE_CONFIG
    DATA = context.DATA
    Path = context.Path
    SUBTITLE_CONFIG = context.SUBTITLE_CONFIG
    WATERMARK_CONFIG = context.WATERMARK_CONFIG
    assemble_elastic_voice_track = context.assemble_elastic_voice_track
    get_azure_keys = context.get_azure_keys
    get_doubao_key = context.get_doubao_key
    get_job = context.get_job
    json = context.json
    DOUBAO_CONFIG = context.DOUBAO_CONFIG
    doubao_synthesize = context.doubao_synthesize
    load_azure_config = context.load_azure_config
    load_doubao_config = context.load_doubao_config
    load_subtitle_config = context.load_subtitle_config
    load_watermark_config = context.load_watermark_config
    make_bilingual_srt = context.make_bilingual_srt
    make_srt = context.make_srt
    make_voice_track = context.make_voice_track
    plan_elastic_timeline = context.plan_elastic_timeline
    render_video = context.render_video
    translation_result = context.translation_result
    update_job = context.update_job
    validate_azure_config = context.validate_azure_config
    validate_azure_key = context.validate_azure_key
    validate_doubao_config = context.validate_doubao_config
    validate_doubao_key = context.validate_doubao_key
    write_wav = context.write_wav
    try:
        job = get_job(job_id)
        engine = job.get("dubbing_engine") or "azure"
        if engine not in ("azure", "doubao"):
            raise RuntimeError("不支持的配音引擎")
        try:
            pipeline = validate_azure_config(load_azure_config(AZURE_CONFIG))
        except ValueError:
            pipeline = validate_azure_config({**context.AZURE_DEFAULTS})
        watermark_settings = load_watermark_config(WATERMARK_CONFIG)
        subtitle_settings = load_subtitle_config(SUBTITLE_CONFIG)
        synthesize_fn = None
        if engine == "doubao":
            doubao = validate_doubao_config(load_doubao_config(DOUBAO_CONFIG))
            key = get_doubao_key()
            if not key:
                raise RuntimeError("尚未配置豆包语音 API Key")
            validate_doubao_key(key)
            keys = [key]
            config = {**pipeline, **doubao, "engine": "doubao", "region": doubao["resource_id"],
                      "voice": doubao["speaker"], "max_synthesis_adjust": 0,
                      "per_key_concurrency": doubao["concurrency"]}
            synthesize_fn = doubao_synthesize
        else:
            keys = get_azure_keys()
            config = {**pipeline, "engine": "azure"}
            if not keys or not config["region"]:
                raise RuntimeError("尚未配置 Azure Speech 密钥和区域")
            for key in keys:
                validate_azure_key(key)
        result = translation_result(job_id)
        all_segments = [item for group in result["groups"] for item in group["segments"]]
        if not all_segments:
            raise RuntimeError("没有可配音的中文句段")
        run = Path(job["run_path"])
        duration = float(job["media"]["duration_seconds"])
        segments = [{**item, "_next_start": (all_segments[index + 1]["start"] if index + 1 < len(all_segments) else duration)}
                    for index, item in enumerate(all_segments) if str(item.get("zh") or "").strip()]
        update_job(job_id, stage="synthesize", dubbing_status="running", dubbing_progress=0,
                   dubbing_total=len(segments), dubbing_error="")
        baseline = {}
        cache_dir = DATA / "dubbing" / job_id
        source_track, _, timings = make_voice_track(
            segments, config, keys, cache_dir, duration,
            progress=lambda count: update_job(job_id, dubbing_progress=count),
            baseline_info=baseline, synthesize_fn=synthesize_fn)
        pieces, cues, retiming, issues, output_duration = plan_elastic_timeline(
            segments, timings, duration, max_adjust=float(config["max_video_adjust"]))
        track = (source_track if issues else assemble_elastic_voice_track(
            segments, timings, config, cache_dir, output_duration))
        voice_path = DATA / f"{job_id}-voice.wav"
        write_wav(voice_path, track)
        affected_segments = len({item.get("id") or item.get("kind") for item in issues})
        report = {"job_id": job_id, "engine": engine, "voice": config["voice"], "region": config["region"],
                  "hd_temperature": config["hd_temperature"],
                  "audio_mode": "separated_background" if job["separate_vocals"] else "voice_only",
                  "segments": len(segments), "skipped_segments": [item["id"] for item in all_segments if not str(item.get("zh") or "").strip()], "issues": issues,
                  "azure_key_count": len(keys),
                  "per_key_concurrency": config["per_key_concurrency"],
                  "affected_segments": affected_segments, "timings": timings,
                  "baseline": baseline,
                  "source_duration_seconds": round(duration, 3),
                  "output_duration_seconds": round(output_duration, 3),
                  "watermark": watermark_settings,
                  "subtitles": subtitle_settings,
                  "retiming": retiming,
                  "policy": {"max_synthesis_adjust": config["max_synthesis_adjust"],
                             "max_post_adjust": config["max_post_adjust"],
                             "max_global_adjust": config["max_global_adjust"],
                             "max_local_adjust": config["max_local_adjust"],
                             "max_video_adjust": config["max_video_adjust"],
                             "video_retiming": "配音长于画面时放慢、短于画面时加快当前句到下句之间的画面，幅度不超过画面调速上限；背景音（如保留）、配音和字幕一起伸缩"}}
        report_path = DATA / f"{job_id}-dubbing-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if issues:
            update_job(job_id, dubbing_status="needs_review", dubbing_error=f"{affected_segments} 句存在时长问题，请下载报告并调整中文台词")
            return
        (DATA / f"{job_id}-dubbed-zh.srt").write_text(make_srt(cues), encoding="utf-8-sig")
        (DATA / f"{job_id}-dubbed-bilingual.srt").write_text(make_bilingual_srt(cues), encoding="utf-8-sig")
        burn_path = DATA / f"{job_id}-dubbed-{'bilingual' if subtitle_settings['mode'] == 'bilingual' else 'zh'}.srt"
        update_job(job_id, stage="render")
        instrumental = run / "separated" / "instrumental.flac" if job["separate_vocals"] else None
        video_encoder = render_video(run / "source.mp4", instrumental,
                     voice_path, DATA / f"{job_id}-dubbed.mp4", output_duration,
                     float(config["background_volume"]),
                     pieces if retiming else None,
                     watermark=watermark_settings,
                     subtitles=({**subtitle_settings, "enabled": False} if not cues else subtitle_settings),
                     subtitle_path=burn_path, log_path=DATA / f"{job_id}.log")
        if isinstance(video_encoder, str):
            report["video_encoder"] = video_encoder
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_job(job_id, stage="done", dubbing_status="done", dubbing_error="")
    except Exception as exc:
        update_job(job_id, dubbing_status="failed", dubbing_error=str(exc))

