"""Translation background processing, independent of HTTP routes."""

from __future__ import annotations


def correct_packet(context, job_id: str, key: str, config: dict, packet: dict, source_digest: str) -> dict:
    CORRECTION_VERSION = context.CORRECTION_VERSION
    DATA = context.DATA
    as_completed = context.as_completed
    copy = context.copy
    correct_english_group = context.correct_english_group
    json = context.json
    make_english_srt = context.make_english_srt
    update_job = context.update_job
    check_pause = context.check_pause
    check_pause(job_id)
    path = DATA / f"{job_id}-corrected-english.json"
    result = {"correction_version": CORRECTION_VERSION, "source_sha256": source_digest,
              "model": config["model"], "groups": [], "usage": {}}
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (saved.get("correction_version") == CORRECTION_VERSION
                and saved.get("source_sha256") == source_digest
                and saved.get("model") == config["model"]):
            result = saved
    completed = {group["id"] for group in result["groups"]}
    update_job(job_id, stage="correct", translation_status="correcting",
               translation_progress=len(completed), translation_total=len(packet["groups"]),
               translation_error="")
    pending = [(index, group) for index, group in enumerate(packet["groups"])
               if group["id"] not in completed]
    first_error = None
    with context.pause_aware_pool(max_workers=int(config["concurrency"])) as pool:
        futures = {}
        for index, group in pending:
            next_context = (packet["groups"][index + 1]["segments"][0]["edited_text"]
                            if index + 1 < len(packet["groups"]) else "")
            futures[pool.submit(correct_english_group, key, config, packet, group, next_context)] = group["id"]
        for future in as_completed(futures):
            check_pause(job_id)
            try:
                corrected, usage = future.result()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                continue
            result["groups"].append({"id": futures[future], "segments": corrected, "usage": usage})
            if usage.get("retry_diagnostics"):
                with (DATA / f"{job_id}.log").open("a", encoding="utf-8") as log:
                    log.write(f"英文校正 {futures[future]}：" + "；".join(usage["retry_diagnostics"]) +
                              ("；重试后仍不完整，已沿用原英文并标记待复核\n"
                               if usage.get("correction_fallback") else "；重试成功\n"))
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                result["usage"][name] = result["usage"].get(name, 0) + usage.get(name, 0)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            update_job(job_id, translation_progress=len(result["groups"]))
    check_pause(job_id)
    if first_error is not None:
        raise first_error
    indexed = {group["id"]: group for group in result["groups"]}
    result["groups"] = [indexed[group["id"]] for group in packet["groups"]]
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    corrected_packet = copy.deepcopy(packet)
    previous = ""
    srt_items = []
    for group in corrected_packet["groups"]:
        group["previous_context"] = previous
        corrections = {item["id"]: item for item in indexed[group["id"]]["segments"]}
        for segment in group["segments"]:
            correction = corrections[segment["id"]]
            segment["edited_text"] = correction["en"]
            if correction.get("uncertain") and "correction_uncertain" not in segment["review_flags"]:
                segment["review_flags"].append("correction_uncertain")
            srt_items.append({"start": segment["start"], "end": segment["end"], "en": correction["en"]})
        previous = group["segments"][-1]["edited_text"]
    (DATA / f"{job_id}-corrected-en.srt").write_text(make_english_srt(srt_items), encoding="utf-8-sig")
    return corrected_packet



def translate_job(context, job_id: str):
    DATA = context.DATA
    DEEPSEEK_CONFIG = context.DEEPSEEK_CONFIG
    GLOSSARY_VERSION = context.GLOSSARY_VERSION
    TRANSLATION_VERSION = context.TRANSLATION_VERSION
    as_completed = context.as_completed
    correct_packet = context.correct_packet
    get_api_key = context.get_api_key
    glossary_candidates = context.glossary_candidates
    hashlib = context.hashlib
    json = context.json
    load_config = context.load_config
    make_bilingual_srt = context.make_bilingual_srt
    make_srt = context.make_srt
    refine_glossary = context.refine_glossary
    translate_group = context.translate_group
    update_job = context.update_job
    check_pause = context.check_pause
    try:
        check_pause(job_id)
        key = get_api_key()
        if not key:
            raise RuntimeError("尚未配置 DeepSeek API Key")
        packet_path = DATA / f"{job_id}-translation-input.json"
        packet_bytes = packet_path.read_bytes()
        packet = json.loads(packet_bytes)
        config = load_config(DEEPSEEK_CONFIG)
        source_digest = hashlib.sha256(packet_bytes).hexdigest()
        packet = correct_packet(job_id, key, config, packet, source_digest)
        digest = hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        glossary_path = DATA / f"{job_id}-glossary.json"
        glossary = None
        if glossary_path.is_file():
            candidate = json.loads(glossary_path.read_text(encoding="utf-8"))
            if (candidate.get("source_sha256") == digest and candidate.get("model") == config["model"]
                    and candidate.get("glossary_version") == GLOSSARY_VERSION):
                glossary = candidate
        if glossary is None:
            update_job(job_id, stage="glossary", translation_status="glossary", translation_progress=0,
                       translation_total=len(packet["groups"]), translation_error="")
            candidates = glossary_candidates(packet)
            subject, auto_terms, usage = refine_glossary(key, config, packet, candidates)
            check_pause(job_id)
            merged = [f"{item['en']} = {item['zh']}" for item in auto_terms]
            glossary = {"glossary_version": GLOSSARY_VERSION, "source_sha256": digest,
                        "model": config["model"], "subject": subject,
                        "candidates": candidates, "auto_terms": auto_terms,
                        "merged_terms": merged, "usage": usage}
            glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
        packet["glossary"] = glossary["merged_terms"]
        packet["subject"] = glossary["subject"]
        glossary_digest = hashlib.sha256(json.dumps(
            {"subject": packet["subject"], "terms": glossary["merged_terms"]},
            ensure_ascii=False).encode("utf-8")).hexdigest()
        output_path = DATA / f"{job_id}-translation.json"
        output = {"schema_version": 1, "job_id": job_id, "model": config["model"],
                  "translation_version": TRANSLATION_VERSION,
                  "source_sha256": digest, "glossary_sha256": glossary_digest,
                  "glossary": glossary["merged_terms"], "groups": [], "usage": {}}
        if output_path.is_file():
            previous = json.loads(output_path.read_text(encoding="utf-8"))
            if (previous.get("source_sha256") == digest and previous.get("model") == config["model"]
                    and previous.get("glossary_sha256") == glossary_digest
                    and previous.get("translation_version") == TRANSLATION_VERSION):
                output = previous
        completed = {group["id"] for group in output["groups"]}
        update_job(job_id, stage="translate", translation_status="running", translation_progress=len(completed),
                   translation_total=len(packet["groups"]), translation_error="")
        pending = [group for group in packet["groups"] if group["id"] not in completed]
        first_error = None
        with context.pause_aware_pool(max_workers=int(config["concurrency"])) as pool:
            futures = {pool.submit(translate_group, key, config, packet, group): group["id"]
                       for group in pending}
            for future in as_completed(futures):
                check_pause(job_id)
                try:
                    translated, usage = future.result()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    continue
                output["groups"].append({"id": futures[future], "segments": translated, "usage": usage})
                for key_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    output["usage"][key_name] = output["usage"].get(key_name, 0) + usage.get(key_name, 0)
                output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                update_job(job_id, translation_progress=len(output["groups"]))
        check_pause(job_id)
        if first_error is not None:
            raise first_error
        indexed = {group["id"]: group for group in output["groups"]}
        output["groups"] = [indexed[group["id"]] for group in packet["groups"]]
        items = [item for group in output["groups"] for item in group["segments"]]
        (DATA / f"{job_id}-zh.srt").write_text(make_srt(items), encoding="utf-8-sig")
        (DATA / f"{job_id}-bilingual.srt").write_text(make_bilingual_srt(items), encoding="utf-8-sig")
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_job(job_id, stage="synthesize", translation_status="done", translation_error="")
    except context.PauseRequested:
        context.mark_paused(job_id)
    except Exception as exc:
        update_job(job_id, translation_status="failed", translation_error=str(exc))

