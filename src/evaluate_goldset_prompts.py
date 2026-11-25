"""Evaluate LLM classification prompts against the manually labeled gold set."""

import argparse
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDSET = ROOT / "data" / "interim" / "goldset" / "goldset_labeled.csv"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "interim" / "llm_evaluation"
PROMPT_VERSION = "classification_v3"

HAZARD_CLASSES = (
    "gunungapi",
    "gempabumi",
    "tsunami",
    "tanah_longsor",
    "likuefaksi",
    "abrasi_pantai",
    "penurunan_tanah",
    "semburan_gas_lumpur",
)


def load_dotenv(path):
    path = Path(path)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_prompt(row, max_chars):
    content = str(row.get("content_clean") or "")[:max_chars]
    title = str(row.get("title_clean") or "")
    return f"""Klasifikasikan artikel berita geologi Indonesia.

KELAS:
- gunungapi
- gempabumi
- tsunami
- tanah_longsor
- likuefaksi
- abrasi_pantai
- penurunan_tanah
- semburan_gas_lumpur

ATURAN:
- relevant=true jika artikel memuat bukti minimal satu kejadian geologi, dampak aktual, atau respons langsung terhadap kejadian/dampak yang disebut.
- Kejadian historis atau retrospektif tetap relevant=true jika isi artikel secara nyata membahas peristiwa yang pernah terjadi dan bukti kejadian tersedia.
- Relevansi mitigasi: artikel tentang penanaman mangrove, penguatan infrastruktur, atau kebijakan dapat relevant=true hanya jika secara eksplisit terkait wilayah, dampak, atau kejadian geologi aktual/historis dalam teks.
- relevant=false untuk pembahasan umum, potensi atau ancaman tanpa kejadian aktual yang dibuktikan, cuaca biasa, kebijakan preventif tanpa dampak/gejala aktual, atau sekadar menyebut istilah bencana sebagai latar.
- relevant=false jika artikel hanya memuat bencana sebagai topik politik, ekonomi, acara tahunan, atau kampanye dan tidak memuat bukti kejadian/dampak yang cukup.
- Beri true pada satu atau beberapa kelas hanya jika setiap kelas didukung kutipan dari isi artikel.
- Jangan menebak kelas dari judul, kata kunci pencarian, kategori sumber, nama situs, atau hubungan sebab-akibat yang tidak dinyatakan.
- Gunungapi=true untuk erupsi, aktivitas guguran/leleran awan panas, atau dampak aktual gunungapi yang dibuktikan. Status peningkatan saja tidak cukup bila tidak ada kejadian/dampak yang nyata.
- Gempabumi=true untuk guncangan atau gempa aktual/historis. Pembahasan probabilitas, mitigasi, atau kajian umum tanpa kejadian aktual tidak cukup.
- Tsunami=true untuk kejadian tsunami aktual/historis atau peristiwa tsunami yang terbukti terjadi. Peringatan, potensi megathrust, mitigasi, atau ancaman semata tidak cukup.
- Tanah longsor=true untuk gerakan tanah yang nyata berbentuk tanah longsor, bukan hanya banjir, ambles, erosi, atau tanah amblas secara umum.
- Likuefaksi=true hanya jika pencairan tanah/likuefaksi secara eksplisit terjadi atau terbukti sebagai dampak guncangan gempa. Potensi, ancaman, atau kata likuefaksi pada pembahasan mitigasi saja tidak cukup.
- Abrasi dan akresi=true hanya untuk abrasi/akresi pantai aktual atau dampak nyata pada kawasan pesisir. Penanaman pohon atau mitigasi dengan mangrove tanpa bukti abrasi aktual tidak cukup.
- Penurunan muka tanah=true hanya jika ada penurunan permukaan tanah/subsidensi aktual/historis atau bukti perubahan elevasi. Kebijakan air tanah yang bersifat preventif tanpa bukti penurunan aktual tidak cukup.
- Semburan gas/lumpur=true untuk semburan gas/lumpur aktual/historis yang dibuktikan. Ancaman gas, risiko gas, atau mitigasi saja tidak cukup.
- Izinkan beberapa kelas bernilai true apabila isi secara eksplisit membuktikan lebih dari satu jenis geologi.
- Gunakan satu atau beberapa kutipan singkat dari isi artikel; kutipan harus persis cocok dengan teks tanpa perubahan, penambahan, atau parafrasa.
- Kembalikan true/false untuk semua delapan kelas.
- evidence_text harus berupa kutipan persis yang ada dalam teks artikel.
- Jangan menggunakan query_group atau hazard_type sebagai bukti.
- confidence berupa angka 0 sampai 1.
- Jika tidak relevan, semua kelas harus false dan event_report_count=0.
- Jawab hanya JSON valid tanpa Markdown.

SCHEMA:
{{
  "relevant": true,
  "classes": {{
    "gunungapi": true,
    "gempabumi": false,
    "tsunami": false,
    "tanah_longsor": false,
    "likuefaksi": false,
    "abrasi_pantai": false,
    "penurunan_tanah": false,
    "semburan_gas_lumpur": false
  }},
  "evidence_text": "kutipan persis",
  "confidence": 0.95
}}

JUDUL:
{title}

ISI:
{content}"""


def post_json(url, headers, payload, timeout, retries=3, retry_delay=10):
    last_error = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code}: {detail[:500]}")
            if exc.code not in {429, 503} or attempt == retries:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"URL error: {exc.reason}")
            if attempt == retries:
                raise last_error from exc
        except http.client.HTTPException as exc:
            last_error = RuntimeError(f"HTTP connection error: {exc}")
            if attempt == retries:
                raise last_error from exc
        time.sleep(retry_delay * (attempt + 1))
    raise last_error


def gemini_generate(prompt, model, api_key, timeout, retries=3, retry_delay=10):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    result = post_json(
        url,
        {"Content-Type": "application/json", "X-goog-api-key": api_key},
        payload,
        timeout,
        retries,
        retry_delay,
    )
    candidates = result.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini tidak mengembalikan candidates: {result}")
    return candidates[0]["content"]["parts"][0]["text"]


def openrouter_generate(prompt, model, api_key, timeout, retries=3, retry_delay=10):
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Kembalikan hanya JSON valid sesuai instruksi pengguna.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    result = post_json(
        "https://openrouter.ai/api/v1/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "http://localhost",
            "X-Title": "Geohazard News Locator",
        },
        payload,
        timeout,
        retries,
        retry_delay,
    )
    return result["choices"][0]["message"]["content"]


def groq_generate(prompt, model, api_key, timeout, retries=3, retry_delay=10):
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "Package groq belum terpasang. Jalankan: pip install groq"
        ) from exc

    client = Groq(api_key=api_key, timeout=timeout)
    last_error = None
    for attempt in range(retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "Kembalikan hanya JSON valid sesuai instruksi pengguna.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_completion_tokens=2048,
                top_p=1,
                response_format={"type": "json_object"},
                stream=False,
            )
            return completion.choices[0].message.content
        except Exception as exc:
            last_error = RuntimeError(str(exc))
            status_code = getattr(exc, "status_code", None)
            if status_code not in {429, 503} or attempt == retries:
                raise last_error from exc
            time.sleep(retry_delay * (attempt + 1))
    raise last_error


def parse_json_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def normalize_prediction(value):
    if not isinstance(value, dict):
        return None
    classes = value.get("classes")
    if not isinstance(classes, dict):
        return None
    normalized_classes = {
        name: bool(classes.get(name, False))
        for name in HAZARD_CLASSES
    }
    confidence = value.get("confidence", 0)
    if not isinstance(confidence, (int, float)):
        confidence = 0
    return {
        "relevant": bool(value.get("relevant", False)),
        "classes": normalized_classes,
        "evidence_text": str(value.get("evidence_text") or ""),
        "confidence": max(0.0, min(1.0, float(confidence))),
    }


def label_is_yes(row, column):
    return str(row.get(column, "")).strip().lower() == "yes"


def validate_evidence(prediction, row):
    """Verify each requested evidence quote is present in the source text."""
    if not prediction["relevant"]:
        return True
    evidence = str(prediction["evidence_text"]).strip()
    if not evidence:
        return False
    content = str(row.get("content_clean") or "")
    pieces = [
        piece.strip()
        for piece in evidence.split(" | ")
        if piece.strip()
    ]
    return bool(pieces) and all(piece in content for piece in pieces)


def metrics_for_predictions(rows, predictions):
    actual_relevant = []
    predicted_relevant = []
    for row in rows:
        article_id = row["article_id"]
        prediction = predictions.get(article_id)
        actual_relevant.append(label_is_yes(row, "relevant"))
        predicted_relevant.append(
            prediction["relevant"] if prediction else False
        )

    metrics = {
        "relevant": binary_metrics(actual_relevant, predicted_relevant),
        "classes": {},
    }
    for name in HAZARD_CLASSES:
        actual = [label_is_yes(row, name) for row in rows]
        predicted = []
        for row in rows:
            prediction = predictions.get(row["article_id"])
            predicted.append(
                bool(prediction["classes"][name]) if prediction else False
            )
        metrics["classes"][name] = binary_metrics(actual, predicted)
    return metrics


def binary_metrics(actual, predicted):
    tp = sum(a and p for a, p in zip(actual, predicted))
    fp = sum(not a and p for a, p in zip(actual, predicted))
    fn = sum(a and not p for a, p in zip(actual, predicted))
    tn = sum(not a and not p for a, p in zip(actual, predicted))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    accuracy = (tp + tn) / len(actual) if actual else 0.0
    return {
        "accuracy": round(accuracy, 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def read_csv(path):
    import csv

    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV tidak memiliki header: {path}")
        required = {"article_id", "title_clean", "content_clean"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"Kolom wajib tidak tersedia di {path}: {', '.join(sorted(missing))}"
            )
        return list(reader)


def cache_path(output_dir, provider, model, prompt_version, article_id, content_hash):
    identity = f"{provider}|{model}|{prompt_version}|{article_id}|{content_hash}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(output_dir) / "llm_cache" / f"{digest}.json"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluasi prompt klasifikasi terhadap gold set."
    )
    parser.add_argument("--goldset", type=Path, default=DEFAULT_GOLDSET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--provider",
        choices=("gemini", "openrouter", "groq"),
        default="gemini",
    )
    parser.add_argument("--model", default="gemini-flash-latest")
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=10)
    args = parser.parse_args(argv)

    if args.provider == "groq":
        if args.limit < 0:
            raise SystemExit("--limit tidak boleh negatif.")
        if args.limit == 0:
            args.limit = min(200, len(read_csv(args.goldset)))
        if args.limit > 1000:
            raise SystemExit(
                "Batas Groq saat ini 1.000 request/hari. "
                "Gunakan --limit 1000 atau kurang."
            )
        if args.sleep < 2.5:
            args.sleep = 2.5
        if args.max_chars > 3000:
            args.max_chars = 3000

    load_dotenv(ROOT / ".env")
    if args.provider == "openrouter" and args.model == "gemini-flash-latest":
        args.model = os.environ.get("OPENROUTER_MODEL", "")
        if not args.model:
            raise SystemExit(
                "Set OPENROUTER_MODEL di .env atau gunakan "
                "--model <openrouter-model-id>"
            )
    if args.provider == "groq" and args.model == "gemini-flash-latest":
        args.model = os.environ.get("GROQ_MODEL", "")
        if not args.model:
            raise SystemExit(
                "Set GROQ_MODEL di .env atau gunakan "
                "--model <groq-model-id>"
            )
    env_key = {
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "groq": "GROQ_API_KEY",
    }[args.provider]
    api_key = os.environ.get(env_key, "")
    if not api_key:
        raise SystemExit(
            f"{env_key} belum disetel. Salin .env.example ke .env "
            "dan isi key pada lingkungan lokal."
        )

    rows = read_csv(args.goldset)
    if args.limit > 0:
        rows = rows[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions = {}
    failures = {}

    for index, row in enumerate(rows, start=1):
        article_id = row["article_id"]
        content_hash = hashlib.sha256(
            str(row.get("content_clean") or "").encode("utf-8")
        ).hexdigest()
        cache_file = cache_path(
            output_dir,
            args.provider,
            args.model,
            PROMPT_VERSION,
            article_id,
            content_hash,
        )
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        if cache_file.is_file():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            predictions[article_id] = cached
            continue

        prompt = build_prompt(row, args.max_chars)
        try:
            if args.provider == "gemini":
                text = gemini_generate(
                    prompt,
                    args.model,
                    api_key,
                    args.timeout,
                    args.retries,
                    args.retry_delay,
                )
            elif args.provider == "openrouter":
                text = openrouter_generate(
                    prompt,
                    args.model,
                    api_key,
                    args.timeout,
                    args.retries,
                    args.retry_delay,
                )
            else:
                text = groq_generate(
                    prompt,
                    args.model,
                    api_key,
                    args.timeout,
                    args.retries,
                    args.retry_delay,
                )
            prediction = normalize_prediction(parse_json_object(text))
            if prediction is None:
                raise ValueError("JSON tidak sesuai schema klasifikasi")
            if not validate_evidence(prediction, row):
                failures[article_id] = "evidence_text tidak ditemukan persis dalam content_clean"
                continue
            predictions[article_id] = prediction
            cache_file.write_text(
                json.dumps(prediction, ensure_ascii=False),
                encoding="utf-8",
            )
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures[article_id] = str(exc)
        print(f"{index}/{len(rows)} {article_id}", flush=True)
        time.sleep(args.sleep)

    evaluation_complete = len(predictions) == len(rows) and not failures
    metrics = (
        metrics_for_predictions(rows, predictions)
        if evaluation_complete
        else None
    )
    report = {
        "provider": args.provider,
        "model": args.model,
        "model_version": f"{args.provider}:{args.model}",
        "prompt_version": PROMPT_VERSION,
        "rows_evaluated": len(rows),
        "predictions": len(predictions),
        "failures": len(failures),
        "coverage": round(len(predictions) / len(rows), 6) if rows else 0.0,
        "evaluation_status": "complete" if evaluation_complete else "incomplete",
        "metrics_warning": (
            None
            if evaluation_complete
            else "Metrik tidak dihitung karena sebagian gold set gagal dipanggil "
            "atau tidak menghasilkan prediksi."
        ),
        "failure_ids": failures,
        "metrics": metrics,
    }
    suffix = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        f"{args.provider}_{args.model}",
    ).strip("_")
    report_path = output_dir / f"evaluation_{suffix}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prediction_path = output_dir / f"predictions_{suffix}.json"
    prediction_path.write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"Laporan: {report_path}")
    print(f"Prediksi: {prediction_path}")


if __name__ == "__main__":
    raise SystemExit(main())
