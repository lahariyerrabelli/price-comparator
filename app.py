"""
app.py  –  Price Comparison Flask Backend
"""

from flask import Flask, render_template, request, jsonify, Response
import threading
import json
import time

from scraper import scrape_all
from matcher import group_and_compare

app = Flask(__name__)

# ── In-memory job store ──────────────────────────────────────────────────────
jobs = {}
job_counter = [0]
job_lock = threading.Lock()


def _clean_offer(offer: dict) -> dict:
    """Strip internal matcher keys; keep product_url."""
    return {k: v for k, v in offer.items() if not k.startswith("_")}


def run_scrape_job(job_id: str, item: str, location: str):
    try:
        def update(msg, pct):
            jobs[job_id]["progress"] = {"message": msg, "pct": pct}

        update("Scraping Blinkit, Zepto & BigBasket simultaneously…", 5)
        blinkit, zepto, bigbasket = scrape_all(item, location)

        print(f"Blinkit: {len(blinkit)} | Zepto: {len(zepto)} | BigBasket: {len(bigbasket)}")

        update("Matching & comparing products…", 85)
        raw_groups = group_and_compare(blinkit, zepto, bigbasket)
        print(f"Groups returned: {len(raw_groups)}")

        groups = []
        for g in raw_groups:
            clean_offers = [_clean_offer(o) for o in g["offers"]]
            clean_best   = _clean_offer(g["best_deal"]) if g["best_deal"] else None
            groups.append({
                "canonical_name": g["canonical_name"],
                "canonical_qty":  g["canonical_qty"],
                "offers":         clean_offers,
                "best_deal":      clean_best,
                "savings":        g["savings"],
            })

        jobs[job_id].update({
            "results":  groups,
            "status":   "done",
            "progress": {"message": "Complete!", "pct": 100},
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        jobs[job_id].update({
            "status": "error",
            "error":  str(e),
        })


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def search():
    data     = request.get_json(force=True)
    item     = (data.get("item")     or "").strip()
    location = (data.get("location") or "Hyderabad").strip()

    if not item:
        return jsonify({"error": "item is required"}), 400

    with job_lock:
        job_counter[0] += 1
        job_id = f"job_{job_counter[0]}"
        jobs[job_id] = {
            "status":   "running",
            "progress": {"message": "Starting…", "pct": 0},
            "results":  None,
            "error":    None,
        }

    t = threading.Thread(target=run_scrape_job, args=(job_id, item, location), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({
        "status":   job["status"],
        "progress": job["progress"],
        "results":  job["results"],
        "error":    job["error"],
    })


@app.route("/api/stream/<job_id>")
def stream(job_id):
    def generate():
        prev_pct = -1
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'unknown job'})}\n\n"
                break

            status_val = job["status"]
            progress   = job["progress"]
            pct        = progress["pct"]

            if status_val == "done":
                yield f"data: {json.dumps({'progress': progress, 'status': 'done', 'results': job['results'], 'error': None})}\n\n"
                break

            if pct != prev_pct:
                prev_pct = pct
                yield f"data: {json.dumps({'progress': progress, 'status': 'running'})}\n\n"

            if status_val == "error":
                yield f"data: {json.dumps({'progress': progress, 'status': 'error', 'results': [], 'error': job['error']})}\n\n"
                break

            time.sleep(0.4)

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)