# Konteks Proyek: Temporal Memory untuk Hermes (`hermes-dev`)

> **Cara pakai:** taruh file ini di root clone dev (`~/dev/hermes-agent-dev/`).
> Jangan ikut di-commit ke branch kerja: tambahkan ke `.git/info/exclude`.
> Mulai sesi agent dengan prompt di bagian **10. Kickoff prompt**.
> Pasangannya: `TEMPORAL_MEMORY_PLAN.md` (fase, checklist, risiko, keputusan).

---

## 1. Peran dan tujuan

Kamu membantu **Iki** mengembangkan fitur **temporal memory** ("AI calendar") untuk Hermes Agent.
Hermes ini adalah **CLI tool personal Iki**. Bukan produk untuk orang lain dan bukan kontribusi upstream.

Konsep dari Iki (jangan diubah tanpa bertanya):

- Hermes membaca percakapan natural dan mengenali informasi yang terikat waktu, lalu mengikatnya ke **tanggal absolut**.
  - "besok gue mau makan sama ina" -> planned event, relatif ke "besok".
  - "dua hari lalu gue kecelakaan" -> kejadian lampau pada tanggal tertentu.
- Referensi yang harus dipahami: hari ini, besok, kemarin, dua hari lalu, minggu depan, minggu lalu, jumat depan, tanggal tertentu, periode tertentu.
- Tujuannya **pemahaman timeline user**, bukan sekadar menyimpan event seperti calendar app. Hermes tahu sesuatu: akan terjadi, sedang terjadi, sudah terjadi, pernah direncanakan, atau berkaitan dengan tanggal/periode tertentu. Informasi itu dipakai lagi saat relevan (follow-up natural).
- **Hemat context window:**
  - *Near-term temporal context* (default): hanya sekitar satu minggu dari sekarang.
  - *Explicit historical retrieval*: hanya dipicu kalau user menyebut tanggal/periode lama ("tanggal 12 september gue ada kejadian apa?", "bulan lalu gue pernah ngomong soal ini nggak?").

## 2. Aturan kerja (wajib)

1. **Tahap saat ini bisa berbeda per fase.** Lihat `TEMPORAL_MEMORY_PLAN.md`. Selama Fase 0, **dilarang mengubah code**. Hanya membaca, menjalankan, dan melaporkan.
2. **Pisahkan FAKTA dan HIPOTESIS** di setiap laporan. Fakta harus punya rujukan `file:baris` atau output command. Jangan menebak behavior yang belum diverifikasi.
3. **Semua keputusan desain masih terbuka** kecuali yang tertulis di bagian 7 sebagai "diputuskan". Jangan memutuskan schema, naming, storage, atau trigger sendiri. Ajukan opsi dan trade-off, lalu tunggu Iki.
4. **Jangan sentuh instalasi harian** (`~/.hermes/`). Semua kerja di clone dev dengan data dev (bagian 3).
5. **Perubahan kecil dan bertahap.** Satu file / satu langkah per giliran, lalu berhenti dan jelaskan. Iki lebih suka pendekatan *teaching-oriented* dan *incremental*, serta file dikirim satu per satu, bukan zip besar.
6. **Ikuti aturan repo** (`AGENTS.md` root dan per-area dimuat otomatis):
   - Jangan mutasi context lama atau system prompt di tengah conversation (prompt caching).
   - Behavior baru masuk ke file sibling, bukan facade.
   - Jangan hardcode `~/.hermes` (pakai `get_hermes_home()`).
   - Test lewat `scripts/run_tests.sh`, **bukan** `pytest` langsung. Test tidak boleh menulis ke `~/.hermes/`.
   - Sebelum selesai: `python scripts/check`.
7. **Ringkas dan jujur.** Kalau ada yang gagal atau tidak bisa diverifikasi, katakan. Jangan klaim "sudah jalan" tanpa menjalankannya.

## 3. Environment

| Hal | Nilai |
|---|---|
| Clone dev | `~/dev/hermes-agent-dev` (branch `temporal-memory`, dari SHA yang sama dengan instalasi harian Iki) |
| Data dev | `HERMES_HOME=$HOME/hermes-dev-data`, `HERMES_RUNTIME_DIR=$HERMES_HOME/tools` |
| Command | `hermes-dev` (wrapper di `~/.local/bin/hermes-dev` -> `scripts/run-in-hermes-env python -m hermes_cli.main`) |
| Instalasi harian | `~/.hermes/hermes-agent`, command `hermes`. **Read-only bagi kamu.** |
| Timezone Iki | WIB (`Asia/Jakarta`), berbasis di Bogor |
| OS | Fedora |

Jangan jalankan gateway dev dengan bot token yang sama dengan instalasi harian (ada mekanisme token lock).
Jangan menyalin atau mencetak isi `.env` ke output, log, atau commit.

## 4. Fakta terverifikasi (dari analisis atas upstream HEAD `6c61c3e2`)

> **Peringatan:** diverifikasi di clone upstream terbaru, **bukan** di checkout lokal Iki.
> Item bertanda (V) wajib diverifikasi ulang di SHA lokal sebelum dijadikan dasar code.

**Storage dan session**
- (V) State di SQLite via `hermes_state.py` (facade) + sibling `hermes_state_*.py`. Schema di `hermes_state_common.py`, `SCHEMA_VERSION = 31`.
- (V) Tabel `messages` punya `timestamp REAL`, `message_uid`, flag `active`/`compacted`, dan FTS5. Tabel `sessions` punya `started_at`.
- (V) `hermes_state_timeline.py` adalah timeline **transkrip** untuk UI. Bukan temporal memory. Hati-hati salah paham nama.

**Agent loop dan injeksi context**
- (V) Turn: `agent/conversation_loop.py::run_conversation` + `agent/turn_*.py`. Persiapan context di `agent/turn_context.py`.
- (V) Konten per-turn disuntik ke **user message**, bukan system prompt: `MemoryManager.prefetch_all()` (dibungkus `<memory-context>`) dan hook plugin `pre_llm_call` (return `{"context": ...}`, cap 10.000 karakter, selebihnya di-spill ke file).
- (V) Invarian: system prompt byte-stable, hanya compression yang boleh memecah cache, role strict alternating.

**Waktu**
- (V) `hermes_time.now()` tz-aware; timezone dari `HERMES_TIMEZONE` atau `timezone:` di `config.yaml`, **fallback ke waktu server**.
- (V) System prompt hanya berisi **tanggal** awal conversation (`Conversation started: ...`), bukan jam. Baris "Today's date (as of the last context rebuild)" hanya muncul saat rebuild. Jadi di session panjang model tidak tahu "hari ini" dengan presisi -> **resolve tanggal harus di code, bukan oleh model.**

**Memory, search, cron**
- (V) Builtin memory (`MEMORY.md`/`USER.md`) = snapshot beku per session, tanpa query waktu.
- (V) `MemoryProvider` ABC (`agent/memory_provider.py`): hanya **satu** external provider aktif; `plugins/memory/` ditutup untuk provider in-tree baru.
- (V) `session_search`: FTS5, `after`/`before` memfilter **waktu mulai session**, bukan tanggal yang disebut di isi pesan.
- (V) Cron: `cron/jobs.py` + `scheduler.py`, tool `cronjob`, one-shot ISO didukung, sesi cron terpisah dan `skip_memory=True`.

**Surface plugin umum**
- (V) `register_hook` (`pre_llm_call`, `post_llm_call` [hanya turn sukses], `on_session_*`), `register_tool`, `register_cli_command`, `plugin_db(name)` (SQLite di `<HERMES_HOME>/plugin-data/<nama>/`), `ctx.llm.complete_structured`, `ctx.dispatch_tool`, `register_auxiliary_task`.
- (V) Hook `pre_llm_call`/`post_llm_call` **tidak** membawa timestamp pesan maupun author.
- (V) Kebijakan repo: plugin tidak boleh mengubah core; core adalah "narrow waist". (Iki memakai ini secara personal, jadi ini lebih ke pertimbangan maintenance saat `hermes update`, bukan syarat upstream.)

## 5. Hipotesis yang HARUS diverifikasi (Fase 0)

- **H1.** Context dari `pre_llm_call` ikut tersimpan di history lewat sidecar `api_content`, sehingga inject tiap turn akan **menumpuk**. (Ada kode: "what turn N sends is what turn N+1 replays".) Dampak besar ke desain injection.
- **H2.** `post_llm_call` bisa mengakses `message_uid` atau id pesan sumber untuk provenance.
- **H3.** Hook plugin berjalan juga di sesi cron dan subagent (default yang diinginkan: **skip** di keduanya).
- **H4.** `python` di dalam `run-in-hermes-env` me-resolve ke Python milik PM sehingga wrapper `hermes-dev` bekerja. Fallback: `source ./activate`.
- **H5.** Gateway menyediakan timestamp pesan platform yang bisa dipakai sebagai anchor (relevan untuk pesan yang tertunda).
- **H6.** Perilaku `hermes update` terhadap perubahan lokal (ada logika autostash di `hermes_cli/update_cmd*.py`).
- **H7.** `python-dateutil` di `uv.lock` hanya dependency transitif (jangan diandalkan; lagipula tidak paham bahasa Indonesia).
- **H8.** `memory.provider` di `config.yaml` Iki kosong/builtin (menentukan apakah opsi external `MemoryProvider` masih layak).
- **H9.** Rewind/branch/compaction tidak membuat data plugin menjadi yatim tanpa mekanisme pembersihan.

## 6. Konsep desain kunci (hasil analisis)

- Ada **dua sumbu waktu**:
  - *said-at*: kapan diucapkan -> sudah ada (`messages.timestamp`, `session_search`).
  - *refers-to*: tanggal yang **dirujuk** -> **belum ada**; inilah inti fitur ini.
- **Resolve tanggal di code**, deterministik, dengan anchor `(waktu pesan, timezone)`. LLM hanya boleh menormalisasi ekspresi (mis. `{unit: day, offset: +1}`), bukan menghitung tanggal.
- Simpan **ekspresi mentah + hasil resolve + presisi** (hari / bagian hari / jam / minggu / bulan).
- Extraction **hybrid**: gate regex/lexicon Indonesia dulu, LLM structured hanya jika gate kena. Gagal = tidak menyimpan, tidak boleh merusak turn (**fail-open**).
- Cron = **trigger**, bukan storage.

## 7. Status keputusan

**Diputuskan oleh Iki:**
- Fitur ini untuk **pemakaian pribadi**, bukan untuk orang lain/upstream.
- Pengembangan di clone dev, dijalankan dengan `hermes-dev`, data terpisah.
- Fase analisis tidak mengubah code.

**Usulan kerja (BELUM diputuskan, minta konfirmasi sebelum Fase 1):**
- Opsi A: **plugin umum standalone** (hook + tool + `plugin_db`), tanpa mengubah core. Alasan praktis: selamat dari `hermes update`.
- Opsi B (external `MemoryProvider`) jadi layak hanya jika H8 benar.
- Opsi C (ubah core + schema `state.db`) **tidak disarankan**: bump `SCHEMA_VERSION` bisa bentrok dengan upstream.

**Masih terbuka:** lihat tabel keputusan di `TEMPORAL_MEMORY_PLAN.md`.

## 8. Di luar scope (kecuali Iki meminta)

Reminder cron proaktif, backfill history lama, event berulang, tanggal Hijriah, group chat, multi-profile, kalender eksternal, kualitas level upstream/catalog.

## 9. Gaya komunikasi dengan Iki

- Bahasa Indonesia, santai (lo/gue boleh), istilah teknis tetap Inggris.
- Jelaskan *kenapa*, bukan hanya *apa*. Iki sedang belajar sambil membangun.
- Satu langkah per giliran. Tunjukkan file satu per satu.
- Kalau ada pilihan desain, beri 2-3 opsi + trade-off + rekomendasi, lalu tanya.

## 10. Kickoff prompt (copy-paste ke agent)

```text
Baca TEMPORAL_MEMORY_CONTEXT.md dan TEMPORAL_MEMORY_PLAN.md di root repo ini,
lalu baca AGENTS.md root dan agent/AGENTS.md serta plugins/AGENTS.md.

Kita ada di FASE 0 (verifikasi). Aturan: JANGAN ubah code apa pun.

Tugas pertama: verifikasi hipotesis H1 sampai H9 satu per satu, mulai dari H1.
Untuk tiap hipotesis laporkan: status (terbukti / terbantahkan / belum bisa),
bukti (file:baris atau output command), dan dampaknya ke desain.

Kerjakan SATU hipotesis per giliran, lalu berhenti dan tunggu respons saya.
Pisahkan FAKTA dan HIPOTESIS. Kalau SHA di repo ini berbeda dari yang tertulis
di konteks (6c61c3e2), sebutkan dulu dan jelaskan apa bedanya.
```
