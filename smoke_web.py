"""Exercise the local API with demo2.mp4. Run while web_app is serving."""

import json
import time
from pathlib import Path

import requests

base = "http://127.0.0.1:8000"
video = Path(__file__).with_name("demo2.mp4")
with video.open("rb") as source:
    response = requests.post(base + "/api/jobs", files={"file": (video.name, source, "video/mp4")},
                             data={"title": "demo2 网页验收", "subject": "单人课程视频", "glossary": "laser = 激光"})
response.raise_for_status()
job_id = response.json()["id"]
print("job", job_id, flush=True)
response = requests.post(f"{base}/api/jobs/{job_id}/run")
response.raise_for_status()
for _ in range(180):
    time.sleep(2)
    job = requests.get(f"{base}/api/jobs/{job_id}").json()
    print(job["status"], job["stage"], flush=True)
    if job["status"] in ("ready", "failed"):
        break
assert job["status"] == "ready", job
assert job["media"]["duration_seconds"] > 0
segments = requests.get(f"{base}/api/jobs/{job_id}/segments").json()
assert segments and all(0 <= s["start"] < s["end"] <= job["media"]["duration_seconds"] + .1 for s in segments)
first = segments[0]
response = requests.patch(f"{base}/api/jobs/{job_id}/segments/{first['id']}",
                          json={"edited_text": first["edited_text"], "reviewed": True})
response.raise_for_status()
packet_response = requests.post(f"{base}/api/jobs/{job_id}/prepare-translation")
packet_response.raise_for_status()
packet = packet_response.json()
assert packet["audio_source"] == "separated_vocals"
assert sum(len(group["segments"]) for group in packet["groups"]) == len(segments)
assert packet["groups"][0]["segments"][0]["raw_text"] == first["raw_text"]
for kind in ("video", "vocals", "instrumental"):
    assert requests.get(f"{base}/api/jobs/{job_id}/media/{kind}", headers={"Range": "bytes=0-255"}).status_code == 206
assert requests.get(f"{base}/api/jobs/{job_id}/translation-input.json").status_code == 200
print(json.dumps({"job_id": job_id, "segments": len(segments), "groups": len(packet["groups"]),
                  "status": "passed"}, ensure_ascii=False), flush=True)
