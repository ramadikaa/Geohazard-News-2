# Prompt Klasifikasi

## classification_v2

Prompt v2 memperluas relevansi agar mencakup mitigasi, kebijakan, tanggapan,
dan respons yang terkait dampak geologi aktual atau historis.

Keunggulan:

- Meningkatkan recall relevansi pada artikel kebijakan/mitigasi.
- Mendeteksi abrasi dan penurunan muka tanah dengan sangat baik pada sampel 45.

Keterbatasan:

- Masih terlalu luas untuk mitigasi dan pembahasan ancaman.
- Tsunami dan likuefaksi mudah terlewat atau salah dikaitkan.
- Kutipan belum divalidasi secara eksplisit oleh evaluator.

## classification_v3

Prompt v3 mempersempit definisi relevansi dan menambahkan aturan per kelas.

Prinsip:

1. Prioritaskan kejadian/dampak aktual atau historis yang dibuktikan.
2. Tetapkan mitigasi atau kebijakan sebagai relevan hanya jika ada bukti
   kejadian/dampak nyata dalam artikel.
3. Jangan memberi label dari judul, keyword pencarian, kategori, atau sumber.
4. Beri label setiap kelas hanya berdasarkan kutipan isi.
5. Kutipan harus tepat ada dalam `content_clean`.
6. Jangan menganggap potensi, status, risiko, atau kajian umum sebagai kejadian.

Aturan spesifik:

- Tsunami: memerlukan kejadian/peristiwa tsunami aktual atau historis.
- Likuefaksi: memerlukan pencairan tanah atau dampak likuefaksi aktual.
- Tanah longsor: memerlukan gerakan tanah berbentuk longsoran.
- Abrasi/akresi: memerlukan abrasi atau akresi aktual/dampak pesisir.
- Subsidence: memerlukan bukti penurunan permukaan aktual/historis.
- Gunungapi: memerlukan erupsi atau dampak aktual gunungapi.
- Gempa: memerlukan guncangan/gempa aktual atau historis.
- Semburan: memerlukan semburan gas/lumpur aktual/historis.

## Validasi output

- `relevant=false`: semua delapan kelas harus false.
- `relevant=true`: minimal satu kutipan persis ada di `content_clean`.
- Kutipan model yang tidak ditemukan persis ditandai sebagai kegagalan
  evidence, bukan dipaksa menjadi prediksi yang dapat dinilai.
- Hasil v3 tidak boleh dijalankan tanpa batas LLM yang baru ditentukan.
