# Konfigurasi API Aman

## Penyimpanan key

Jangan menaruh API key di source code, perintah shell yang disimpan, atau `git`.

1. Salin `.env.example` menjadi `.env`.
2. Isi key melalui editor lokal.
3. Jangan menjalankan atau membagikan isi `.env`.
4. Rotasi semua key yang pernah dipaste ke percakapan atau log.
5. Batasi key ke model/API yang diperlukan dan aktifkan batas penggunaan bila tersedia.

PowerShell:

```powershell
$env:GEMINI_API_KEY = Read-Host "Gemini API key"
python src/evaluate_goldset_prompts.py --provider gemini --model gemini-flash-latest
python src/evaluate_goldset_prompts.py --provider openrouter --model perplexity/pplx-decider-v1.3:free
python src/evaluate_goldset_prompts.py --provider groq --model meta-llama/llama-prompt-guard-2-22m
```

Alternatif lokal: isi `.env` dengan variabel yang tersedia; skrip memuatnya tanpa mencetak nilainya.

## Cache

Hasil prediksi disimpan di `data/interim/llm_evaluation/llm_cache/` dan sudah dikecualikan Git. Cache key dibuat dari provider, model, prompt, article ID, dan hash konten.

## File evaluasi

Setiap provider menghasilkan:

- `evaluation_<provider>_<model>.json`
- `predictions_<provider>_<model>.json`

Jangan menghapus laporan; catat `model_version` dan `prompt_version`.
