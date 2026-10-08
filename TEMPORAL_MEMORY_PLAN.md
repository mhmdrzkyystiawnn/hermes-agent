# Rencana: Temporal Memory untuk Hermes (personal, `hermes-dev`)

Pasangan dari `TEMPORAL_MEMORY_CONTEXT.md`. File ini adalah **dokumen hidup**: centang item,
ubah status keputusan, dan catat temuan di bagian **Log**.

Legenda status: `[ ]` belum, `[~]` berjalan, `[x]` selesai, `[!]` terblokir.

---

## Fase -1: Persiapan environment

Tujuan: punya clone dev yang identik dengan instalasi harian, terisolasi, dan bisa dijalankan.

- [x] Cek `~/.hermes/hermes-agent` adalah git checkout; catat `git rev-parse HEAD` dan `git status --short` → HEAD `27c02f6326e82b2ba184f5a849f0c5b3a38efb9c`, tree bersih
- [x] Putuskan: perubahan lokal (jika ada) ikut dibawa atau tidak → tidak ada perubahan lokal
- [x] `git clone ~/.hermes/hermes-agent ~/projects/hermes-agent-dev`, lalu `git checkout -b temporal-memory <SHA>` (catatan: path `~/projects/`, bukan `~/dev/`)
- [x] Tambahkan `TEMPORAL_MEMORY_*.md` ke `.git/info/exclude`
- [x] Buat `~/hermes-dev-data`; config minimal ditulis manual (TIDAK menyalin config/.env harian); MiniMax key ditambah belakangan
- [x] Pastikan `timezone: Asia/Jakarta` di `config.yaml` dev
- [~] `source ./activate` (provision toolchain) → **dilewati**: `setup-hermes.sh` butuh unduh uv+python; dipakai interpreter venv harian langsung
- [x] Buat wrapper `~/.local/bin/hermes-dev`, `chmod +x`, pastikan `~/.local/bin` ada di `PATH`
- [x] `hermes-dev --version` / `--help` jalan; sesi one-shot & 3-turn jalan (MiniMax-M2.7)
- [x] Pastikan `hermes` harian **tidak** terpengaruh (config.yaml/auth.json mtime tidak berubah; `~/.hermes/plugins/` tetap bersih)

**Selesai jika:** `hermes-dev` jalan terisolasi dan `hermes` harian utuh. → **TERPENUHI**

---

## Fase 0: Verifikasi hipotesis (TANPA ubah code)

Dikerjakan di `hermes-dev`, satu hipotesis per giliran, hasil dicatat di Log.

| # | Hipotesis | Cara verifikasi | Status | Dampak jika terbukti |
|---|---|---|---|---|
| H1 | Context `pre_llm_call` tersimpan di history & menumpuk antar-turn | Plugin eksperimen kecil yang inject penanda unik; cek `api_content` dan isi `messages` setelah 3 turn | `[x]` **TERBUKTI** | Perlu strategi inject sekali / delta-only |
| H2 | `post_llm_call` bisa dapat `message_uid` | Baca `hermes_cli/plugins_dispatch.py` + payload hook sebenarnya | `[x]` **TERJAWAB (via `conversation_history`)** | Provenance via uid vs via `session_id` + timestamp |
| H3 | Hook jalan di cron/subagent | Jalankan job cron & delegasi dengan hook logger | `[x]` **TERBUKTI (firing di keduanya)** | Perlu guard skip |
| H4 | Wrapper `hermes-dev` benar | Jalankan wrapper; cek `which python` di dalam env | `[x]` **selesai** (cara berbeda) | Fallback ke `activate` |
| H5 | Timestamp pesan platform tersedia di gateway | Baca `gateway/run_*.py` dan session | `[x]` **TERBUKTI (kode+test; tanpa bot live)** | Anchor lebih akurat untuk pesan tertunda |
| H6 | Perilaku `hermes update` pada perubahan lokal | Baca `update_cmd*.py`; uji di clone sekali pakai | `[x]` **TERJAWAB (kode; tanpa menjalankan update)** | Strategi rebase / patch file |
| H7 | `dateutil` hanya transitif | `uv.lock` + `pyproject.toml` | `[x]` **TERBUKTI transitif** | Tulis resolver sendiri (rencana default) |
| H8 | `memory.provider` kosong/builtin | Baca `config.yaml` Iki | `[x]` **TERBUKTI kosong (builtin)** | Membuka opsi B |
| H9 | Rewind/branch/compaction vs data plugin | Baca `hermes_state_rewind.py`, `on_session_switch` | `[x]` **TERJAWAB** | Kebijakan item yatim |

Output Fase 0: ringkasan satu halaman -> keputusan final desain injection dan storage.

### Ringkasan Fase 0 (2026-10-07)

**Semua H terjawab.** Ringkas:

| # | Status | Inti temuan |
|---|---|---|
| H1 | TERBUKTI | Context `pre_llm_call` tersimpan di `api_content` DAN **menumpuk linear** tiap turn (bukti: payload API turn 3 membawa 3 marker). |
| H2 | TERJAWAB | `post_llm_call` tak punya kwarg `message_uid`, tapi `conversation_history[-1]` punya `message_uid`, `_row_id`, `timestamp`, `api_content`. |
| H3 | TERBUKTI | Hook firing di cli/cron/subagent; `platform` tersedia untuk guard. |
| H4 | Selesai | `hermes-dev` jalan (via interpreter venv harian + PYTHONPATH; bukan `run-in-hermes-env`). |
| H5 | TERBUKTI | Timestamp platform asli tersedia; Hermes sudah punya mesin `message_timestamps` (default OFF). |
| H6 | TERJAWAB | Update pakai autostash; dev clone `origin` lokal + tanpa upstream tracking. |
| H7 | TERBUKTI | `python-dateutil` transitif saja → resolver tulis sendiri. |
| H8 | TERBUKTI | `memory.provider` kosong (builtin) → slot external provider bebas. |
| H9 | TERJAWAB | Rewind/compaction = soft-delete (`active=0`/`compacted=1`), baris+uid tetap ada; **tidak ada hook plugin untuk rewind**. |

**Implikasi desain (fakta → konsekuensi):**

1. **Injection wajib anti-akumulasi.** Karena inject ikut tersimpan di `api_content` dan di-replay tiap turn, inject naif = prompt membengkak O(n²). Opsi: (a) inject hanya saat state temporal berubah; (b) inject snapshot pendek ber-cap tiap turn (linear tapi kecil); (c) inject sekali di awal sesi (murah, tapi basi di turn jauh).
2. **Provenance murah:** `conversation_history[-1].message_uid` + `.timestamp` (waktu platform!) — tidak perlu query DB.
3. **Guard skip:** `platform in {"cron","subagent"}`.
4. **Anchor waktu:** `messages.timestamp` (waktu kirim platform), bukan waktu proses.
5. **Rewind/compaction:** simpan `message_uid` sumber; filter item yang uid-nya sudah tidak aktif (query `messages`). Sinyal bersih hanya lewat jalur `MemoryProvider.on_session_switch(rewound=True)` (Opsi B).
6. **Jangan duplikasi `said-at`:** sudah ada di core (`messages.timestamp` + `message_timestamps` renderer). Fitur ini fokus sumbu **refers-to**.
7. **Wrapper dev** tidak lewat `run-in-hermes-env` (butuh unduh uv+python). Kalau nanti mau aktivasi PM normal, jaringan harus stabil.

**Selesai jika:** semua H terjawab dengan bukti, dan Iki menyetujui arah (A/B/C).

---

## Fase 1: MVP (plugin; nol perubahan core)

> Mulai hanya setelah Fase 0 selesai dan keputusan arsitektur dikonfirmasi Iki.
> Struktur plugin dan nama final **belum diputuskan**.
> **D1 = Opsi B (external MemoryProvider)** — dikonfirmasi 2026-10-07.
> **Langkah 1 SELESAI** (kerangka + aktivasi). Lanjut ke langkah 2 (resolver tanggal).

Urutan kerja, **satu langkah per giliran**:

0. **[x] Kerangka provider + aktivasi** (SELESAI 2026-10-07)
   - Provider `temporal` di `~/hermes-dev-data/plugins/temporal/` (kind memory provider)
   - `memory.provider: temporal` di config dev; `hermes-dev memory status` → active
   - Lifecycle terbukti: `initialize` → `sync_turn` → `on_session_end` → `shutdown`
   - Guard cron/subagent terbukti: `agent_context="cron"` → `active=false`, `sync_turn` tidak firing
   - `h1_logger` (scaffolding Fase 0) di-disable

1. **Resolver tanggal** (fungsi murni, tanpa I/O)
   - [x] Input: `(ekspresi, anchor_datetime, tz)`; output: `Resolution(start, end, precision, ambiguous, alternatives)` — SELESAI 2026-10-07
   - [x] Lexicon Indonesia: hari ini, besok, lusa, kemarin, kemarin lusa, N hari lalu/lagi, minggu depan/lalu, bulan depan/lalu, nama hari, tanggal eksplisit, bagian hari (pagi/siang/sore/malam, tadi malam)
   - [x] Tabel tes golden (lihat di bawah) dengan anchor tetap → `test_golden.py` **25/25 lulus**
   - Catatan: file di `~/hermes-dev-data/plugins/temporal/{resolver.py,test_golden.py}`. Keputusan produk sebagai konstanta (`WEEK_STARTS_ON_MONDAY`, `WEEKDAY_DEPAN_IS_NEAREST`, `BARE_DAY_IS_FUTURE`) — ganti konstanta, jalankan tes. Ambigu **dikembalikan** (`ambiguous=True` + `alternatives`), bukan disembunyikan.
2. **Storage** (`plugin_db`)
   - [x] Satu tabel `temporal_items` (+ `PRAGMA user_version=1`, TANPA tabel meta) — SELESAI 2026-10-07
   - [x] Index pada `time_start` (+ `idx_temporal_window`, `idx_temporal_src_msg`, `idx_temporal_session`)
   - [x] Simpan ekspresi mentah, hasil resolve, presisi, status, sumber (session/uid/row_id), confidence
   - [x] Interval **INKLUSIF dua ujung** `[start, end]` (konsisten dengan resolver: `+1 hari - 1µs`)
   - [x] Rewind: **keanggotaan** `source_message_id ∈ active ids`, BUKAN batas `MAX(id)`
   - Catatan: `~/hermes-dev-data/plugins/temporal/storage.py` + `test_storage.py` (49 cek, ALL PASSED). `plugin_db` → `row_factory=None` (tuple). CRUD sekarang: `init_schema`, `insert_item`, `get_item`, `items_in_window`, `items_overlapping`, `mark_stale`, `active_source_ids`.
3. **Extraction**
   - [x] Gate regex/lexicon (`extract.gate`, 16 pola; "dua hari lalu" ikut) — SELESAI 2026-10-07
   - [x] LLM structured hanya jika gate kena (`agent.auxiliary_client.call_llm` task `temporal_extract`; klasifikasi rencana/lampau/ongoing + epistemic + ringkasan + ekspresi mentah)
   - [x] Resolve di code (resolver), simpan via storage — `items` terbukti di run nyata
   - [x] Fail-open (LLM error/timeout/JSON rusak/conn rusak → 0, tidak melempar)
   - [x] Skip di cron/subagent (`agent_context` guard, sudah terbukti di langkah 1)
   - [x] **LIFECYCLE PINDAH KE `on_session_end` (opsi 3, disetujui Iki).** `sync_turn` kini HANYA gate+log (A2), tanpa LLM. Extraction utama di `on_session_end(messages)` yang dipanggil SINKRON oleh `MemoryManager._each_provider` (`agent/memory_manager.py:667`), SEBELUM `shutdown_all()` → drain 5s → jadi di luar `_SYNC_DRAIN_TIMEOUT_S`. **Balapan drain teratasi.**
   - [x] Dedup `(source_session_id, source_message_id)` (idempotensi, menutup panggilan ganda F5 & session restart) — terbukti lintas-instance
   - [x] **BATCH SIZE, bukan session cap (D1–D5, disetujui Iki).** `MAX_EXTRACTIONS_PER_SESSION` kini bermakna **ukuran batch** (=5; alias `EXTRACT_BATCH_SIZE`). `on_session_end` → `enqueue` SEMUA kandidat gate ke tabel durable `temporal_pending` (**commit sebelum LLM**), lalu `drain_batch` 1 batch (5, **FIFO**). `on_turn_start` → drain 1 batch lagi. Ulang sampai pending habis. **Tidak ada kandidat yang hilang** — baris pending hanya dihapus setelah extraction SUKSES; LLM gagal → tetap pending (retry lifecycle berikutnya). Provenance/dedup tetap `(source_session_id, source_message_id)`. Guard cron/subagent/flush tetap 0. Skema `user_version` 1 → **2** (tabel kedua `temporal_pending`).
   - **Bukti test batch (offline, LLM mock):** 12 kandidat → `on_session_end` 5 item + 7 pending; turn berikutnya +5 (10 item, 2 pending); turn berikutnya +2 (12 item, **0 pending**). FIFO terbukti (baris 100–104 dulu). Restart (instance baru) drain sisa 2 tanpa duplikasi. Failure 1 kandidat → tetap pending (baris 301), retry sukses → 3 item, 0 pending. Idempotensi `on_session_end` 2× → item & pending tidak bertambah. Guard cron/subagent/flush = 0 item & 0 pending.
   - **Watchdog NYATA 30s, 12 kandidat (LLM nyata):** 3 run, total **items+pending = 12 SELALU** (tidak ada yang hilang). `drained` bervariasi 3–4 dari 5 karena rate-limit MiniMax (`failed` 1–2) — yang gagal **tetap pending**, persis failure semantics. Durasi `on_session_end` 16–20s (< 30s).
   - **Migrasi DB v1 → v2:** `init_schema` pada DB lama menaikkan `user_version` 1→2 dan membuat `temporal_pending` **tanpa menyentuh** 11 item lama (terbukti).
   - **E2E 5× sesi 2-turn:** **10/10 item, pending=0** tiap run. Probe DB akhir: `temporal_pending` = 0 baris.
   - **Regresi:** resolver 25/25 · storage ALL PASSED · extract ALL PASSED · session_end ALL PASSED (45 cek).
   - **Risiko tersisa (dikoreksi):** **R5 = hard-exit / `os._exit(0)` SAJA** (watchdog 30s, kanban worker `os._exit`) yang bisa melewati provider hook sebelum `on_session_end` berjalan → kandidat sesi itu belum ter-enqueue. **Ctrl+C interactive BUKAN R5:** `MemoryProvider.on_session_end` SUDAH terpanggil lewat cleanup normal (`_tui_shutdown` → `_run_cleanup` → `_shutdown_agent_memory_provider`); one-shot juga menutup provider (`_close_agent`). Catatan: `_emit_interrupted_session_end` hanya memanggil **plugin hook** `on_session_end` (subsistem berbeda), bukan `MemoryProvider.on_session_end` — itu sebabnya R5 sempat terlihat seperti bug. **R1** hard-kill sebelum enqueue → ditutup backfill Fase 2 (A3). Margin watchdog tipis (puncak ~25s vs 30s) → non-fatal: baris pending sudah commit sebelum LLM, jadi batch diulang lifecycle berikutnya.
   - Catatan penting: `ctx.llm` TIDAK tersedia untuk memory provider (`_ProviderCollector` hanya meneruskan `register_*`); seam = `agent.auxiliary_client.call_llm` (preseden `plugins/memory/query_rewrite.py`). MiniMax-M2.7 butuh `extra_body.reasoning_split: true` atau JSON-nya terpotong thinking.
4. **Injection near-term** (`prefetch()` — BUKAN `system_prompt_block`, BUKAN `pre_llm_call`)
   - [x] Hook = `prefetch()`; core mem-fence otomatis (`build_memory_context_block`) → provider kembalikan **teks polos**. Terbukti coexist dgn builtin (gabung `\n\n`, satu fence).
   - [x] Window default **±7 hari inklusif**, anchor = wall-clock `now` di tz sesi (Asia/Jakarta). Overlap inklusif via `storage.items_in_window` (tanpa SQL paralel).
   - [x] Cap **8 item** + **~400 char** (whole-line, deterministik; header ikut dihitung). Format: `- YYYY-MM-DD (precision) summary [kind] [epistemic] [ambigu] [?]`.
   - [x] `stale=1` → skip; `status=active` → tampil; `cancelled/superseded` → skip default, tampil bila user minta (D5). `ambiguous=1` → `[ambigu]`; `confidence<0.5` → `[?]` (D4, tidak dibuang).
   - [x] Historical (D3/D6, deterministik tanpa LLM): butuh **referensi lampau + sinyal retrieval** ("apa/kapan/masih ingat/…"). Ekspresi di-resolve `resolver` → interval absolut → query overlap. Fallback lookback 365 hari **hanya** bila target tak bisa di-resolve.
   - [x] `recall_status()` mencerminkan prefetch terakhir (indikator 🧠).
   - [x] Fail-open: DB/conn error → `""`, tak melempar. Tidak ada item → `""` (tanpa blok kosong).
   - **Bukti test:** `test_recall.py` ALL PASSED (near-term, boundary ±7d, overlap, stale, cancelled default/asked, ambiguity, low-conf, historical resolve + explicit date + fallback, D6 statement-vs-question, cap 8, char cap, empty→"", fail-open, count, snapshot bound).
   - **E2E nyata:** sesi A simpan `besok makan sama ina` → sesi B `prefetch` recall **1 item, 103 char** (events.log).
   - **D1 terukur (jujur):** snapshot per-turn **bounded** (≤400 char), TAPI core **me-replay** tiap sidecar → total replay tetap **O(n²)**. Bukti DB `api_content`: satu sesi 5-turn tumbuh +342 → +382 → +437 → +474; sesi berurutan +421/+366/+326/+329. Ini trade-off yang diterima; **tidak** mengubah core.
   - **Regresi:** resolver 25/25 · storage · extract · session_end · recall ALL PASSED. Core 0 file.
5. **Retrieval historis** (`temporal_recall` tool)
   - [x] Tool `temporal_recall` di provider (`get_tool_schemas` + `handle_tool_call`), ter-routing via `MemoryManager` (terbukti `has_tool` + dispatch). Argumen: `expression` (wajib), `include_cancelled` (opsional). Deterministik: `resolver.resolve` → `items_in_window`/`items_overlapping` → JSON (tanggal absolut + provenance `source_session_id`/`source_message_id`/`uid`). Tak bisa di-resolve → `{"matched": false}`. Fail-open.
   - [x] Bukti E2E: seed item pada range resolver `tanggal 12 september` → tool `matched=true, count=1`, provenance `42/sess-tool2`; `cancelled` tersembunyi default (count 1), tampil dengan `include_cancelled=true` (count 2).
   - [ ] Gabung `said-at` (`session_search`) — belum; di luar scope MVP.
6. **Operasional**
   - [x] CLI `hermes temporal list|delete` via `cli.py` provider (`register_cli(subparser)` + handler `temporal_command`, ter-discovery `discover_plugin_cli_commands`). `list`: id + range absolut + summary + kind/status/epistemic + `[STALE:reason]`/`[ambigu]`; filter `--range D[..D]`, `--status`, `--no-stale`, `--limit`; status selalu terlihat (cancelled tidak diam-diam aktif). `delete <id>`: eksplisit by id, error jelas bila tak ada (exit 1), non-integer ditolak (exit 2); **tidak menyentuh `temporal_pending`**. Storage CRUD baru: `list_items`/`delete_item`/`count_items`.
   - [x] Bukti E2E CLI: `hermes-dev temporal list` menampilkan 2 item (id, range, kind/status); `delete 2` → `deleted temporal item 2` (exit 0); `delete 999` → `error: no temporal item with id 999` (exit 1); `delete abc` → error non-integer; pending tetap 0.
   - [x] Regresi: resolver 25/25 · storage · extract · session_end · recall · **ops** ALL PASSED. Core 0 file.
   - [ ] `scripts/run_tests.sh` repo-wide — belum dijalankan (test plugin mandiri).

**Selesai jika:** skenario ini jalan di `hermes-dev`:
1. Hari 1: "besok gue makan sama ina" -> tersimpan dengan tanggal absolut yang benar.
2. Hari 2: chat baru -> Hermes bisa menyinggung rencana itu secara natural, tanpa inject yang menggembung.
3. "tanggal 12 september gue ada kejadian apa?" -> `temporal_recall` bekerja.

**Sengaja di luar MVP:** reminder cron, backfill, event berulang, Hijriah, group chat.

---

## Fase 2: Penyempurnaan

- [ ] Siklus status (planned -> done / cancelled / superseded), deteksi "nggak jadi", "dipindah ke ..."
- [ ] Dedupe kejadian yang disebut berkali-kali
- [ ] Follow-up pasif: item `planned` yang waktunya lewat disinggung sekali di chat berikutnya
- [ ] Backfill dari `messages.timestamp` (opsional, manual)
- [ ] Reminder cron opt-in via `dispatch_tool("cronjob")` (isi self-contained, karena sesi cron `skip_memory`)
- [ ] Item `ongoing` ("lagi sakit sejak kemarin")

## Fase 3: Evaluasi

- [ ] Pakai sendiri 1-2 minggu; catat false positive (event palsu) dan false negative
- [ ] Putuskan: tetap plugin, atau ada seam yang butuh patch core kecil (simpan sebagai `.patch` terpisah)
- [ ] Rencana rebase/update terhadap upstream

---

## Tabel keputusan

| # | Keputusan | Opsi | Status | Catatan |
|---|---|---|---|---|
| D1 | Arsitektur | A plugin / B external provider / C core | **DIPUTUSKAN: B (external MemoryProvider)** | Jalur discovery user-dir TERVERIFIKASI empirik |
| D2 | Apa yang disimpan | Semua / hanya actionable | Terbuka | Kejadian pribadi tetap lewat provider LLM |
| D3 | Konfirmasi | Diam-diam / tanya dulu | Terbuka | Pengaruh ke akurasi vs rasa "creepy" |
| D4 | Proaktif | Hanya saat Iki chat / kirim duluan | Terbuka | Default aman: pasif |
| D5 | "Jumat depan" | Jumat terdekat / Jumat minggu depan | Terbuka | Lihat tabel golden |
| D6 | "besok" dini hari (mis. 01.00) | Kalender / setelah tidur | Terbuka | |
| D7 | Awal pekan | Senin / Minggu | Terbuka (usulan: Senin) | Mempengaruhi "minggu depan/lalu" |
| D8 | Window near-term | +/-7 hari simetris / asimetris | Terbuka | Usulan awal Iki: ~1 minggu |
| D9 | Backfill | Ya / mulai dari nol | Terbuka | Fase 2 |
| D10 | Model extraction | Model utama / auxiliary murah | Terbuka | Biaya ditanggung Iki |
| D11 | Personal-only | -- | **Diputuskan** | Tidak untuk upstream/orang lain |
| D12 | Dev di clone + `hermes-dev` | -- | **Diputuskan** | Data terpisah |

---

## Risiko dan mitigasi

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Context menumpuk di history (H1) | Prompt membengkak, biaya naik | Verifikasi di Fase 0; inject delta/sekali; budget kecil |
| Salah resolve tanggal | Rencana salah hari | Resolver deterministik + tes golden; simpan ekspresi mentah; flag ambigu |
| Timezone salah | Semua tanggal geser | `timezone: Asia/Jakarta` wajib (fallback bisa waktu server) |
| Biaya/latensi LLM extraction | Lambat/mahal | Gate regex, background, model murah |
| Extraction gagal | Turn rusak | Fail-open |
| Event palsu (hipotetis, cerita orang lain, negasi) | Memory kotor | Klasifikasi LLM + status + command hapus |
| Data pribadi masuk ke provider LLM | Privasi | Keputusan D2; simpan minimal |
| Item yatim setelah rewind/compaction (H9) | Data basi | Simpan uid sumber; kebijakan pembersihan |
| `hermes update` bentrok dengan edit core | Konflik merge | Plugin-first; patch core disimpan sebagai `.patch` |
| Bump `SCHEMA_VERSION` (opsi C) | DB nyangkut saat upstream migrasi | Hindari opsi C |
| Merusak instalasi harian | Kehilangan data | `HERMES_HOME` terpisah; jangan bagi token gateway |
| Agent memodifikasi dirinya sendiri yang sedang jalan | Alat rusak saat dipakai memperbaiki | Edit pakai `hermes` harian/Claude Code; `hermes-dev` hanya target uji |
| Prompt injection lewat teks tersimpan yang di-inject balik | Perilaku tak diinginkan | Fence + sanitasi seperti `<memory-context>` |

---

## Tabel golden resolver (draf awal)

Anchor: **Rabu, 2026-10-07 10:00 WIB**. Awal pekan diasumsikan **Senin** (D7). Nilai bertanda **?** menunggu keputusan.

| Ekspresi | Hasil yang diharapkan | Presisi |
|---|---|---|
| hari ini | 2026-10-07 | hari |
| besok | 2026-10-08 | hari |
| lusa | 2026-10-09 | hari |
| kemarin | 2026-10-06 | hari |
| kemarin lusa / dua hari lalu | 2026-10-05 | hari |
| tadi malam | 2026-10-06 malam (rentang kasar) | bagian hari |
| besok sore | 2026-10-08 sore | bagian hari |
| minggu depan | 2026-10-12 s.d. 2026-10-18 | minggu |
| minggu lalu | 2026-09-28 s.d. 2026-10-04 | minggu |
| bulan lalu | 2026-09-01 s.d. 2026-09-30 | bulan |
| tanggal 12 september | 2026-09-12 (lampau, tahun berjalan) | hari |
| jumat depan | **?** 2026-10-09 atau 2026-10-16 (D5) | hari, ambigu |
| tanggal 12 (tanpa bulan) | **?** 2026-10-12 (terdekat ke depan) atau 2026-09-12 | hari, ambigu |
| 12/9 | 2026-09-12 (format d/m) | hari |
| bsk / kmrn | sama dengan besok / kemarin | hari |

Tambahkan kasus baru di sini setiap kali ada ekspresi yang salah di pemakaian nyata.

---

## Log (isi saat bekerja)

| Tanggal | Fase | Temuan / keputusan |
|---|---|---|
| 2026-10-07 | Analisis | Analisis arsitektur selesai atas upstream `6c61c3e2`. Personal-only dan dev clone diputuskan. |
| 2026-10-07 | Fase -1 | Clone dev dibuat di `~/projects/hermes-agent-dev` (bukan `~/dev`), branch `temporal-memory` @ `27c02f6326e82b2ba184f5a849f0c5b3a38efb9c` (= HEAD harian saat itu, BUKAN `6c61c3e2`). Data dev `~/hermes-dev-data`. |
| 2026-10-07 | Fase -1 | Wrapper `hermes-dev` TIDAK lewat `run-in-hermes-env` (butuh unduh uv+python; jaringan sempat blokir). Pakai interpreter venv harian + `PYTHONPATH` ke clone. Catatan: path `tools/python-3.14.7+202****0901-...` punya `*` literal → `execve` gagal kalau lewat variabel; dipakai path venv (`installs/.../venv/bin/python3`) yang bersih. |
| 2026-10-07 | Fase 0 / H1 | **H1 TERBUKTI.** Plugin probe `h1_logger` (`~/hermes-dev-data/plugins/h1_logger/`, kind `standalone`). Bukti: (a) `messages.api_content` = `content` + `\n\n[H1_PROBE ...]`; (b) `pre_api_request.request_messages` menunjukkan marker uuid tiap turn **ter-replay**: turn 3 mengirim 3 marker (turn1+turn2+turn3), total occurrences 1→2→3. Jadi context `pre_llm_call` menumpuk linear antar-turn. |
| 2026-10-07 | Fase 0 / API plugin | `register_hook(name, callback)` = 2 argumen (bukan decorator). `kind` valid: standalone/backend/exclusive/platform/model-provider (bukan `general`). Callback dipanggil `invoke_hook(name, **kwargs)` → harus punya `**kwargs`. |
| 2026-10-07 | Fase 0 / H2 | **H2 TERJAWAB.** `post_llm_call` TIDAK menerima `message_uid` sebagai kwarg (firing site `agent/turn_finalizer.py:466`: session_id, task_id, turn_id, user_message, assistant_response, conversation_history, model, platform). TAPI `conversation_history` membawa dict pesan ber-`message_uid` (+ `_row_id`, `_db_persisted`, `api_content`), dan pada turn berjalan baris user-nya SUDAH punya uid (`rows_with_uid=2` di turn 2). Jadi provenance = `conversation_history[-1].message_uid`. Catatan: `pre_api_request.request_messages` TIDAK membawa uid (hanya `[content, role]`). |
| 2026-10-07 | Fase 0 / H1 lanjutan | Detail penting: `pre_llm_call` menerima `conversation_history` TANPA context yang di-inject (marker tidak muncul), tapi `api_content`/`request_messages` memuatnya. Jadi untuk deteksi penumpukan, acuan = `api_content`, bukan `conversation_history`. |
| 2026-10-07 | Dev model | Dev memakai MiniMax-M2.7 via route OpenAI-compat `https://api.minimax.io/v1` (named provider `minimax-openai` di config dev; key di `~/hermes-dev-data/.env`). Dibuktikan: `GET /v1/models` 200, `POST /v1/chat/completions` 200. |
| 2026-10-07 | Fase 0 / H3 | **H3 TERBUKTI.** Hook (`pre_llm_call`/`pre_api_request`/`post_llm_call`/`on_session_*`) **firing** di sesi `cli`, `cron`, DAN `subagent`. `_persist_disabled` tidak di-set oleh cron maupun subagent (hanya `background_review.py:1022`). Kode: cron `platform="cron"` + `skip_memory=False` (`cron/scheduler.py:2505`); subagent `platform="subagent"` + `skip_memory=True` + `side_agent=True` (`tools/delegate_tool.py:270`). Guard skip yang tersedia: `platform in {"cron","subagent"}` (terbukti ada di payload `pre_llm_call`). Bukti empirik: session id `cron_7ace1914b69a_...` dan `20261007_192329_c9c6b7`. Job test sudah dihapus. |
| 2026-10-07 | Fase 0 / H5 | **H5 TERBUKTI (kode + test; tidak diuji dengan bot live).** (a) `MessageEvent.timestamp` (`gateway/platforms/event.py:96`) diisi waktu platform asli oleh adapter: Telegram `message.date` (`telegram/adapter.py:7209`), Discord `message.created_at` (`discord/adapter.py:6211`), Signal epoch-ms (`signal.py:477`), Buzz `created_at`. Feishu/IRC/WeCom pakai `datetime.now()` (bukan waktu platform). (b) **Sudah ada mesin temporal-awareness bawaan**: `gateway/message_timestamps.py` + `gateway/run.py::_build_gateway_agent_history(inject_timestamps=...)` merender prefix `[Tue 2026-08-20 12:00:00 UTC]` ke pesan user; gate `gateway.message_timestamps.enabled` (**default OFF**, `config_defaults.py:2208`). (c) Disimpan sebagai kolom `messages.timestamp` (`gateway/run_turn.py:1814`), BUKAN di wire payload — `tests/gateway/test_timestamp_sidecar_replay.py:140` menegakkan `timestamp`/`api_content` tidak bocor ke request. (d) Anti-akumulasi sudah ada (`strip_leading_message_timestamps` + `render_user_content_with_timestamp`). **Implikasi desain:** jangan duplikasi `said-at` — fitur ini fokus `refers-to`. Untuk pesan tertunda, anchor = `messages.timestamp` (waktu platform), bukan waktu proses. |
| 2026-10-07 | Fase 0 / H6 | **H6 TERJAWAB (baca kode; update tidak dijalankan).** `hermes update` menangani perubahan lokal via autostash: `_stash_local_changes_if_needed` (`update_cmd_stash.py:82`) → `git stash push --include-untracked -m hermes-update-autostash-<ts>` sebelum pull, lalu `_restore_stashed_changes` (`:432`) meng-apply balik setelah update; stash di-drop kalau sukses. Kalau konflik/gagal → stash **diparkir** (tidak di-drop) + notice. Mode non-interaktif diatur `updates.non_interactive_local_changes` (restore/park/discard). Autostash orphan >7 hari diperingatkan (`_AUTOSTASH_WARN_AGE_DAYS`). **PENTING (khusus setup dev kita):** dev clone punya `origin = /home/iki/.hermes/hermes-agent` (lokal), bukan GitHub, dan branch `temporal-memory` TIDAK punya upstream tracking → `hermes update` di clone dev tidak akan menarik upstream seperti instalasi harian. Update aman: commit dulu di branch, jangan andalkan autostash. |
| 2026-10-07 | Fase 0 / H7 | **H7 TERBUKTI transitif.** `python-dateutil` TIDAK ada di `pyproject.toml`; muncul di `uv.lock` hanya sebagai dependency paket lain (botocore, croniter, daytona-api-client, ...). Jadi tidak bisa diandalkan (lagipula tidak paham bahasa Indonesia) → resolver tanggal ditulis sendiri (sesuai rencana default). |
| 2026-10-07 | Fase 0 / H8 | **H8 TERBUKTI.** Blok `memory:` di `~/.hermes/config.yaml` (baris 1006) hanya berisi `memory_enabled: true`, `user_profile_enabled: true`, char limits, `nudge_interval` — **TIDAK ada `memory.provider`** → provider = builtin (MEMORY.md/USER.md). Jadi slot external `MemoryProvider` **masih kosong/kosong-terpakai** → Opsi B (external provider) secara teknis layak kalau diinginkan. |
| 2026-10-07 | Fase 0 / H9 | **H9 TERJAWAB.** (a) **Rewind** (`/undo`) = soft-delete: `rewind_to_message` set `active=0` untuk id >= target (`hermes_state_messages.py:1854`); **baris + `message_uid` tetap ada**, tidak dihapus. (b) **Compaction** = `archive_and_compact` (`hermes_state_messages.py:976`) set `compacted=1` (baris lama), bukan hapus. (c) **Tidak ada hook plugin khusus rewind.** Plugin punya: `on_session_reset` (payload `session_id`, `reason`, `platform`, `old_session_id`, `new_session_id` — `gateway/slash_commands_session.py:213`), `on_session_finalize` (`cli_session_mixin.py:434`), dan — **khusus MemoryProvider** — `on_session_switch(new_session_id, parent_session_id, reset, rewound)` (`agent/memory_manager.py:704`) yang dipanggil pada `/undo` dengan `rewound=True` (`cli_session_mixin.py:823`) dan pada `/branch`/`/reset`/`/new`/compression dengan `reset=True`. (d) Branch/reset session punya `parent_session_id`. **Implikasi desain:** general plugin TIDAK dapat sinyal rewind/compaction → item temporal memory yang menunjuk baris yang di-rewind bisa jadi yatim. Mitigasi: simpan `message_uid` + timestamp sumber, lalu filter item yang uid-nya sudah `active=0`/`compacted=1` (butuh query ke `messages`). Kalau mau sinyal bersih, jalur `MemoryProvider.on_session_switch(rewound=True)` tersedia tapi menuntut jadi external provider (Opsi B). |
| 2026-10-07 | Fase 0 / D1 | **KEPUTUSAN IKI: Opsi B (external MemoryProvider).** Jalur discovery provider non-bundled **TERVERIFIKASI empirik**: provider probe minimal di `$HERMES_HOME/plugins/<name>/` terdeteksi oleh `plugins.memory.list_memory_provider_names()` → `find_provider_dir()` → `load_memory_provider()` (dimuat sebagai `_hermes_user_memory.<name>__source_<hash>`). Syarat: direktori dengan `__init__.py` yang menyebut `MemoryProvider`/`register_memory_provider`, nama tidak diawali `_`/`.`, dan TIDAK menabrak nama bundled (bundled menang). Aktivasi via `memory.provider: <name>` di config. Catatan: `_is_memory_provider_dir` = heuristik teks 8KB pertama `__init__.py`. |
| 2026-10-07 | Fase 0 / kontrak B | **Kontrak MemoryProvider (`agent/memory_provider.py`).** Wajib: `name` (property), `is_available()`, `initialize(session_id, **kwargs)`, `get_tool_schemas()`. Opsional penting: `prefetch(query, session_id)` (ingat: HARUS cepat/cached, ada timeout), `queue_prefetch`, `sync_turn(user, assistant, messages=..., turn_author=...)`, `on_session_end`, `on_session_switch(rewound=...)`, `on_pre_compress`, `get_config_schema`, `save_config`, `on_memory_write`. **Guard cron/subagent:** `initialize()` menerima `agent_context` = `platform` bila platform ∈ {cron, subagent}, else `"primary"` (`agent_init.py:1297`) → pakai ini, bukan `platform` di hook. Builtin (MEMORY.md/USER.md) tetap aktif berdampingan; hanya SATU external provider boleh aktif (`memory_manager.py:380`). |
| 2026-10-07 | Fase 1 / langkah 1 | **SELESAI.** Provider `temporal` (`~/hermes-dev-data/plugins/temporal/{__init__.py,plugin.yaml}`) — skeleton inert, kind memory provider. Aktivasi `memory.provider: temporal` → `hermes-dev memory status` menunjukkan `temporal (no setup needed) ← active`. **Lifecycle terbukti empirik**: `initialize` → `sync_turn` → `on_session_end` → `shutdown` (log `~/hermes-dev-data/plugin-data/temporal/events.log`). **Guard terbukti**: sesi cron → `agent_context="cron"`, `active=false`, dan `sync_turn` TIDAK firing. `h1_logger` di-disable (config dev: `plugins.disabled: [h1_logger]`). Catatan: provider user dimuat sebagai `_hermes_user_memory.temporal__source_<hash>`; `system_prompt_block()` sengaja kosong (invarian cache). |
| 2026-10-07 | Fase 1 / langkah 2 | **SELESAI.** `resolver.py` (fungsi murni, tanpa I/O) + `test_golden.py` **25/25 lulus** (22 tabel golden PLAN + 3 cross-timezone). Keputusan produk jadi konstanta: `WEEK_STARTS_ON_MONDAY=True` (D7), `WEEKDAY_DEPAN_IS_NEAREST=True` (D5), `BARE_DAY_IS_FUTURE=True`. Ambigu dikembalikan (`ambiguous=True` + `alternatives`), mis. "jumat depan" (9 vs 16 Okt) dan "tanggal 12". **Bug ditemukan & diperbaiki:** hari kalender anchor harus dihitung di timezone TARGET (`anchor.astimezone(tz).date()`), bukan `anchor.date()` — anchor UTC 7 Okt 20:00 = 8 Okt WIB. Dua baris golden cross-tz ditambah agar tidak regresi. Naive anchor (tanpa tz) ditolak `ValueError`. "seminggu lalu" (tanpa spasi) sengaja belum didukung → unmatched. |
| 2026-10-07 | Fase 1 / langkah 2b (storage) | **SELESAI.** `storage.py` + `test_storage.py` (**49 cek, ALL PASSED**). Tiga revisi atas review Iki: (1) `temporal_meta` **dihapus** → versi schema di `PRAGMA user_version`; (2) **interval INKLUSIF dua ujung** `[start,end]` (konsisten dgn resolver, contoh test diperbaiki dari `[10:00,11:00)`); (3) **rewind = keanggotaan**, bukan `MAX(id)`. **Bukti empirik bahwa `MAX(id)` salah** (test case B): `rewind_to_message(preserve_compaction_handoff=True)` menyisipkan baris pengganti dengan id LEBIH TINGGI dari target (`replacement > target`), jadi batas `MAX(id)` meleset — sedangkan keanggotaan `source_message_id ∈ active ids` tetap benar di kedua kasus. Ini juga alasan `source_message_id` disimpan. **Konsekuensi:** provider membaca `state.db` core read-only saat rewind (`active_source_ids()`), gagal → `None` (skip, tidak menebak). Kolom interpretasi (`kind`/`status`/`epistemic`/`confidence`) ada dengan default aman; diisi di langkah 3. |
| 2026-10-07 | Fase 1 / langkah 3 (opsi 1) | **Opsil 1 diterapkan, TAPI belum stabil — langkah 3 BELUM selesai.** Perubahan: `auxiliary.temporal_extract.model = MiniMax-M2.7-highspeed`, `extra_body = {reasoning_split: true, thinking: {type: disabled}}`, `timeout: 20`; `extract.LLM_MAX_TOKENS` 400 → 120. **Tidak ada core yang diubah** (repo bersih; `_SYNC_DRAIN_TIMEOUT_S` tidak disentuh). **Pengukuran latency nyata** (seam `call_llm`, prompt ekstraksi sebenarnya, thinking disabled + reasoning_split): M2.7 4.0–19.8s (avg 9.6); **M2.7-highspeed 2.9–10.7s (avg 5.7)**; M2.5-highspeed 4.2–13.3s; M2.1-highspeed 2.8–14.6s. Semua varian completion 150–590 token. Dengan `max_tokens=120`: 6/6 JSON valid, 3.9–7.8s (masih >5s kadang). **Multi-turn drain test** (5× sesi 2-turn, tiap turn ekspresi waktu berbeda): run1 1/2 (3.9s), run2 1/2 (4.12s), run3 2/2 (5.8s, 4.58s), run4 1/2 (4.26s), run5 2/2 (3.98s, 3.27s) → **6/10 item (40% hilang)**. E2E tambahan: 1/2 hilang. **Kesimpulan: opsi 1 menurunkan tapi tidak menghilangkan kehilangan; risiko kehilangan turn MASIH ADA.** Tes: resolver 25/25, storage ALL PASSED, extract ALL PASSED, fail-open langsung (timeout/JSON invalid/conn failure → 0, tanpa raise) OK. |
