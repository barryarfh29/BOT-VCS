# 🎬 Telegram Live Stream Userbot

Userbot Telegram untuk menjalankan live streaming menggunakan file video ke voice chat/live di grup atau channel.

## Persyaratan

- **Python 3.10+**
- **FFmpeg** (harus terinstall dan ada di PATH)
- **Akun Telegram** (bukan bot, tapi akun user biasa)
- **API ID & API Hash** dari [my.telegram.org](https://my.telegram.org)

## Instalasi

### 1. Install FFmpeg

**Windows:**
```bash
# Menggunakan Chocolatey
choco install ffmpeg

# Atau download manual dari https://ffmpeg.org/download.html
# Lalu tambahkan ke PATH
```

**Linux:**
```bash
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

### 2. Install Dependencies Python

```bash
pip install -r requirements.txt
```

### 3. Konfigurasi

1. Buka [my.telegram.org](https://my.telegram.org)
2. Login dengan nomor telepon
3. Buat aplikasi baru, dapatkan **API ID** dan **API Hash**
4. Copy `.env.example` menjadi `.env` dan isi kredensial:

```bash
copy .env.example .env
```

Edit file `.env`:
```
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
SESSION_NAME=live_stream_bot
```

### 4. Set Environment Variables

**Windows (CMD):**
```cmd
set API_ID=12345678
set API_HASH=abcdef1234567890abcdef1234567890
```

**Windows (PowerShell):**
```powershell
$env:API_ID = "12345678"
$env:API_HASH = "abcdef1234567890abcdef1234567890"
```

**Linux/macOS:**
```bash
export API_ID=12345678
export API_HASH=abcdef1234567890abcdef1234567890
```

### 5. Tambahkan Video

Letakkan file video ke folder `videos/`:
```
videos/
├── video1.mp4
├── concert.mkv
└── music.webm
```

Format yang didukung: `.mp4`, `.mkv`, `.avi`, `.webm`, `.mov`

## Menjalankan

```bash
python bot.py
```

Saat pertama kali dijalankan, akan diminta login (nomor telepon + kode OTP).

## Cara Pakai

Ketik perintah di chat Telegram mana saja (ke diri sendiri/Saved Messages):

| Perintah | Fungsi |
|----------|--------|
| `!live <chat> <video>` | Mulai live streaming video |
| `!stop <chat>` | Stop streaming |
| `!pause <chat>` | Pause streaming |
| `!resume <chat>` | Resume streaming |
| `!playlist` | Lihat daftar video tersedia |
| `!help` | Tampilkan bantuan |

### Contoh:

```
!live @mychannel video.mp4
!live -1001234567890 concert.mkv
!stop @mychannel
!pause -1001234567890
!playlist
```

## Catatan Penting

1. **Voice Chat harus aktif** — Buka voice chat/live di grup/channel terlebih dahulu sebelum menjalankan `!live`
2. **Akun harus admin** — Akun userbot harus memiliki izin "Manage Voice Chats" di grup/channel target
3. **FFmpeg wajib** — Pastikan `ffmpeg` terinstall dan bisa diakses dari terminal
4. **Satu stream per chat** — Hanya bisa satu video streaming per grup/channel

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| `ffmpeg not found` | Install FFmpeg dan pastikan ada di PATH |
| `No active group call` | Buka voice chat di grup/channel dulu |
| `Permission denied` | Pastikan akun punya izin admin |
| `File not found` | Cek nama file dan pastikan ada di folder `videos/` |

## Lisensi

MIT License — Gunakan dengan bijak dan bertanggung jawab.
