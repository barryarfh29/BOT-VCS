/**
 * API Client untuk Web Admin Frontend
 * 
 * Sistem "tanam URL": frontend auto-detect API URL dari backend.
 * Ganti VPS/domain? Cukup update env BASE_URL di server — frontend ikut otomatis.
 * 
 * CARA PAKAI:
 * 1. Set API_BASE_URL di bawah ke IP/domain VPS kamu (sekali saja)
 *    ATAU biarkan kosong dan panggil initApiUrl() saat app load.
 * 2. Semua fungsi api() di bawah otomatis pakai URL yang benar.
 * 
 * PINDAH VPS:
 * - Update env BASE_URL di server baru
 * - Update API_BASE_URL di sini (atau kalau frontend & backend satu server, auto-detect)
 */

// ============================================================
// CONFIG — Ganti IP/domain VPS di sini (satu tempat saja)
// ============================================================

let API_BASE_URL = ""; // Kosongkan kalau mau auto-detect, atau isi: "http://IP_VPS:8080"

/**
 * Auto-detect API URL dari backend /api/config endpoint.
 * Panggil ini saat app pertama kali load.
 * Berguna kalau frontend di-serve dari server yang sama.
 */
export async function initApiUrl(serverUrl = null) {
    if (API_BASE_URL) return API_BASE_URL;
    
    // Kalau frontend dan backend satu server, pakai origin yang sama
    const base = serverUrl || window.location.origin;
    try {
        const resp = await fetch(`${base}/api/config`);
        const data = await resp.json();
        API_BASE_URL = data.api_url || base;
    } catch {
        // Fallback ke origin saat ini
        API_BASE_URL = base;
    }
    return API_BASE_URL;
}

/**
 * Set API URL manual (kalau tahu pasti).
 */
export function setApiUrl(url) {
    API_BASE_URL = url.replace(/\/$/, "");
}

/**
 * Get current API URL.
 */
export function getApiUrl() {
    return API_BASE_URL;
}

// ============================================================
// HTTP HELPER
// ============================================================

function getToken() {
    return localStorage.getItem("admin_token") || "";
}

export function setToken(token) {
    localStorage.setItem("admin_token", token);
}

async function api(method, path, body = null, isFormData = false) {
    const url = `${API_BASE_URL}${path}`;
    const headers = {
        Authorization: `Bearer ${getToken()}`,
    };
    if (!isFormData) {
        headers["Content-Type"] = "application/json";
    }

    const options = { method, headers };
    if (body) {
        options.body = isFormData ? body : JSON.stringify(body);
    }

    const resp = await fetch(url, options);
    if (resp.status === 429) {
        throw new Error("Rate limited. Coba lagi nanti.");
    }
    if (resp.status === 401) {
        throw new Error("Unauthorized. Check token.");
    }
    return resp;
}

// ============================================================
// API FUNCTIONS
// ============================================================

// Auth
export async function ping() {
    const r = await api("GET", "/api/ping");
    return r.json();
}

// Talents
export async function getTalents() {
    const r = await api("GET", "/api/talents");
    return r.json();
}

export async function getTalent(id) {
    const r = await api("GET", `/api/talents/${id}`);
    return r.json();
}

export async function createTalent(data) {
    const r = await api("POST", "/api/talents", data);
    return r.json();
}

export async function updateTalent(id, data) {
    const r = await api("PUT", `/api/talents/${id}`, data);
    return r.json();
}

export async function deleteTalent(id) {
    const r = await api("DELETE", `/api/talents/${id}`);
    return r.json();
}

export async function uploadTalentPhoto(id, file) {
    const form = new FormData();
    form.append("file", file);
    const r = await api("POST", `/api/talents/${id}/photo`, form, true);
    return r.json();
}

export async function uploadTalentVideo(id, file) {
    const form = new FormData();
    form.append("file", file);
    const r = await api("POST", `/api/talents/${id}/videos`, form, true);
    return r.json();
}

export function getTalentPhotoUrl(id) {
    return `${API_BASE_URL}/api/talents/${id}/photo?token=${getToken()}`;
}

export function getTalentVideoUrl(id, index) {
    return `${API_BASE_URL}/api/talents/${id}/videos/${index}/file?token=${getToken()}`;
}

export async function updateVideoClip(talentId, index, data) {
    const r = await api("PUT", `/api/talents/${talentId}/videos/${index}`, data);
    return r.json();
}

export async function deleteVideo(talentId, index) {
    const r = await api("DELETE", `/api/talents/${talentId}/videos/${index}`);
    return r.json();
}

// Templates
export async function getTemplates() {
    const r = await api("GET", "/api/templates");
    return r.json();
}

export async function updateTemplate(key, content) {
    const r = await api("PUT", `/api/templates/${key}`, { content });
    return r.json();
}

// Settings
export async function getSettings() {
    const r = await api("GET", "/api/settings");
    return r.json();
}

export async function updateSettings(data) {
    const r = await api("PUT", "/api/settings", data);
    return r.json();
}

// Admins
export async function getAdmins() {
    const r = await api("GET", "/api/admins");
    return r.json();
}

export async function addAdmin(userId) {
    const r = await api("POST", "/api/admins", { user_id: userId });
    return r.json();
}

export async function removeAdmin(userId) {
    const r = await api("DELETE", `/api/admins/${userId}`);
    return r.json();
}

// Login (Userbot/Talent)
export async function loginSendCode(target, phone, talentId = null) {
    const r = await api("POST", "/api/login/send-code", { target, phone, talent_id: talentId });
    return r.json();
}

export async function loginVerifyOtp(loginId, code) {
    const r = await api("POST", "/api/login/verify-otp", { login_id: loginId, code });
    return r.json();
}

export async function loginVerify2fa(loginId, password) {
    const r = await api("POST", "/api/login/verify-2fa", { login_id: loginId, password });
    return r.json();
}

// Userbot
export async function getUserbotStatus() {
    const r = await api("GET", "/api/userbot/status");
    return r.json();
}

// Transactions
export async function getTransactions() {
    const r = await api("GET", "/api/transactions");
    return r.json();
}

// Activities
export async function getActivities(limit = 50, category = null) {
    let path = `/api/activities?limit=${limit}`;
    if (category) path += `&category=${category}`;
    const r = await api("GET", path);
    return r.json();
}
