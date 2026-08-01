# 🚀 Panduan Deploy & Pindah VPS

Checklist lengkap untuk deploy bot ke VPS baru dan menyambungkan kembali frontend (Vercel).

> **Penting:** Database menggunakan **MongoDB Atlas (cloud)**, bukan di VPS.
> Semua data (talent, transaksi, template, activity log, session userbot) otomatis tersedia
> di VPS baru selama `MONGO_URI` yang dipakai sama. **Tidak perlu migrasi data.**

---

## Arsitektur Singkat

```
[User Telegram] ──► [Bot di VPS :8080 API] ◄── [Frontend Vercel]
                          │
                          ▼
                  [MongoDB Atlas (cloud)]
```

Titik koneksi frontend ↔ backend hanya **satu**: env var `NEXT_PUBLIC_API_URL` di Vercel.

---

## 1. Persiapan VPS Baru

```bash
# Update sistem & install Docker
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
```

## 2. Clone & Konfigurasi Bot

```bash
git clone https://github.com/barryarfh29/BOT-VCS.git
cd BOT-VCS
```

Buat file `.env` (JANGAN commit file ini ke git):

```env
API_ID=...                    # dari my.telegram.org
API_HASH=...                  # dari my.telegram.org
BOT_TOKEN=...                 # dari @BotFather
MONGO_URI=...                 # connection string MongoDB Atlas (SAMA dengan VPS lama)
PAYMENT_API_URL=https://botpayment.site/api/v1
PAYMENT_API_KEY=...           # API key payment gateway
PAYMENT_SECRET=...            # secret payment gateway
```

> Session userbot tersimpan di MongoDB (`settings.userbot_session_string`),
> jadi userbot otomatis login kembali di VPS baru tanpa perlu OTP ulang.

## 3. Build & Jalankan

```bash
docker build -t bot-vcs .
docker run -d --name bot-vcs \
  --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -v $(pwd)/videos:/app/videos \
  bot-vcs
```

Cek log:

```bash
docker logs -f bot-vcs
# Tunggu sampai muncul: "API server: http://0.0.0.0:8080"
```

## 4. Reverse Proxy + HTTPS (Caddy)

Frontend Vercel wajib akses API lewat **HTTPS**. Cara termudah pakai Caddy + sslip.io
(sertifikat SSL otomatis, tanpa perlu domain sendiri):

```bash
apt install -y caddy
```

Edit `/etc/caddy/Caddyfile` (ganti `IP_VPS_BARU` dengan IP publik VPS, pakai tanda minus):

```
IP-VPS-BARU.sslip.io {
    reverse_proxy localhost:8080
}
```

> Contoh: IP `1.2.3.4` → tulis `1-2-3-4.sslip.io` atau `1.2.3.4.sslip.io` (dua-duanya valid).

```bash
systemctl restart caddy
```

Tes dari browser: `https://IP-VPS-BARU.sslip.io/api/templates` → harus balas JSON (401 Unauthorized itu normal, artinya API hidup).

## 5. Sambungkan Frontend (Vercel)

1. Buka [vercel.com/dashboard](https://vercel.com/dashboard) → project **frontend-vcs**
2. **Settings → Environments → Production → Environment Variables**
3. Edit `NEXT_PUBLIC_API_URL` → isi URL baru, contoh: `https://1.2.3.4.sslip.io`
4. **Redeploy**: tab **Deployments** → ⋯ pada deployment teratas → **Redeploy**
   (wajib — env var Next.js dibake saat build)
5. Buka `https://frontend-vcs.vercel.app` → data harus muncul lagi

## 6. Matikan VPS Lama

Setelah VPS baru terbukti jalan normal:

```bash
# di VPS lama
docker stop bot-vcs
```

> Jangan jalankan bot di 2 VPS sekaligus — bot Telegram & userbot akan konflik
> (rebutan update dan session).

---

## 💡 Tips: Pakai Domain Sendiri

Kalau punya domain (misal `bot-ku.com`), arahkan A record `api.bot-ku.com` → IP VPS,
lalu set `NEXT_PUBLIC_API_URL=https://api.bot-ku.com` di Vercel.

Keuntungan: saat ganti VPS berikutnya, **cukup ubah DNS A record** ke IP baru —
Vercel tidak perlu disentuh/redeploy sama sekali.

---

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| Frontend kosong / error fetch | Cek `NEXT_PUBLIC_API_URL` di Vercel benar & sudah Redeploy |
| `https://...sslip.io` tidak bisa diakses | Cek Caddy jalan (`systemctl status caddy`) & port 80/443 terbuka di firewall VPS |
| Bot tidak balas /start | Cek `docker logs bot-vcs`, pastikan `BOT_TOKEN` benar |
| Userbot tidak aktif | Login ulang via bot: Setting → 📱 Login Userbot |
| MongoDB timeout | Whitelist IP VPS baru di MongoDB Atlas → Network Access (atau set 0.0.0.0/0) |
| Activity log kosong | Normal jika bot baru start — data terisi seiring kegiatan berjalan |
