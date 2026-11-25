# Panduan Anotasi Gold Set

## Tujuan

Gold set berisi 200 artikel untuk mengevaluasi prompt klasifikasi, bukan untuk melatih model. Gunakan label yang konsisten dan catat ketidakpastian.

## Berkas

- `data/interim/goldset/goldset_template.csv`: template yang diisi anotator.
- `data/interim/goldset/goldset_source_sample.csv`: data sumber lengkap sampel.
- `data/interim/goldset/goldset_sampling_report.json`: distribusi sampel dan seed.

## Kolom utama

### relevant

Isi `yes` atau `no`.

- `yes`: artikel membahas satu atau lebih dari delapan jenis bencana geologi sebagai kejadian, dampak, atau respons terhadap kejadian.
- `no`: artikel sekadar menyebut istilah bencana, membahas kebijakan umum, cuaca tanpa bencana, atau tidak memuat kejadian geologi.

### Kolom delapan jenis bencana

Isi `yes` atau `no` untuk setiap kelas.

- `gunungapi`
- `gempabumi`
- `tsunami`
- `tanah_longsor`
- `likuefaksi`
- `abrasi_pantai`
- `penurunan_tanah`
- `semburan_gas_lumpur`

Satu artikel boleh memiliki beberapa label. Gunakan bukti eksplisit dalam artikel, bukan hanya asal kueri scraper.

### evidence_text

Salin kutipan singkat dari `content_clean` yang mendukung label relevansi dan jenis bencana. Kutipan harus persis ada pada teks. Untuk satu artikel dapat ditulis beberapa kutipan dipisahkan dengan ` | `.

### event_report_count

Perkiraan jumlah Event Report yang dapat diekstrak dari artikel:

- `0`: tidak relevan.
- `1`: satu kejadian utama.
- `>1`: beberapa kejadian yang dapat dibedakan.
- kosong: belum yakin.

Angka ini digunakan untuk evaluasi pemahaman konsep, bukan untuk membuat Incident pada tahap ini.

### goldset_uncertainty

Isi `low`, `medium`, atau `high`.

- `low`: judul dan isi memberi bukti jelas.
- `medium`: masih memerlukan interpretasi terbatas.
- `high`: berita beragam/ambigu atau bukti tidak cukup.

### annotation_notes

Catatan singkat untuk kasus ambigu, misalnya hubungan sebab-akibat, kegagalan kutipan, atau dugaan peristiwa lama.

## Pedoman keputusan

1. Jangan mengandalkan `query_group` atau `hazard_type` sebagai kebenaran.
2. Putuskan berdasarkan judul dan isi artikel.
3. Untuk gempa, bedakan laporan kejadian nyata dengan pembahasan mitigasi tanpa kejadian.
4. Untuk tsunami, bedakan peringatan/risiko dari kejadian tsunami.
5. Untuk tanah longsor, tambahkan `tanah_longsor` jika bukti menunjukkan gerakan tanah yang secara fisik berupa longsoran.
6. Untuk likuefaksi, label hanya jika pencairan tanah/likuefaksi disebut atau dibuktikan, bukan sekadar gempa dengan kerusakan tanah.
7. Untuk abrasi dan akresi, gunakan label `abrasi_pantai` bila abrasi atau akresi pantai menjadi kejadian nyata.
8. Untuk penurunan muka tanah, gunakan bukti perubahan permukaan tanah atau subsidensi aktual.
9. Untuk semburan gas/lumpur, hanya label jika ada semburan aktual, bukan pembahasan fasilitas yang berisiko.
10. Jika artikel membahas bencana sebagai akibat kejadian lain, label jenis bencana tetap diisi untuk setiap bencana yang dibuktikan.

## Format nilai

- Gunakan hanya `yes`, `no`, atau kosong untuk kolom klasifikasi.
- Simpan kutipan persis tanpa menulis ringkasan sebagai `evidence_text`.
- Jangan mengubah kolom asal, terutama `article_id`, `url_normalized`, dan `content_clean`.
- Jangan menghapus baris; tuliskan ketidakpastian pada kolom terkait.

## Pembagian kerja

1. Anotator mengisi salinan independen dari `goldset_template.csv`.
2. Setelah selesai, gabungkan hasil berdasarkan `article_id`.
3. Artikel dengan ketidaksetujuan atau `goldset_uncertainty=high` masuk review kedua.
4. Simpan hasil final sebagai `goldset_labeled.csv`.
