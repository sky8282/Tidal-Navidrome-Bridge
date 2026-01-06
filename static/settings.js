document.addEventListener('DOMContentLoaded', () => {
    if(document.getElementById('login-modal') && document.getElementById('login-modal').classList.contains('is-visible')) {
        return;
    }
    initNavidromeConfig();
    initTidalAuth();
    initDeleteButtons();
});

async function initNavidromeConfig() {
    const urlInput = document.getElementById('nav-url');
    const userInput = document.getElementById('nav-user');
    const passInput = document.getElementById('nav-pass');
    const saveBtn = document.getElementById('save-nav-btn');
    const userBadge = document.getElementById('nav-user-badge');
    if (!saveBtn) return;
    try {
        const res = await fetch('/settings/navidrome');
        if (res.ok) {
            const data = await res.json();
            if (data.nav_url) {
                urlInput.placeholder = "*****已配置*****";
                userInput.placeholder = "*****已配置*****";
                passInput.placeholder = "*****已配置*****";
            }
            if (data.nav_username) {
                userBadge.style.display = 'none';
            } else {
                userBadge.style.display = 'none';
            }
            
        }
    } catch (e) {
        console.error("Failed to load nav settings", e);
    }
    saveBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        const user = userInput.value.trim();
        const pass = passInput.value.trim();
        if (!url) {
            alert("请输入 Navidrome 服务器地址");
            return;
        }
        saveBtn.disabled = true;
        saveBtn.textContent = "保存中...";
        try {
            const res = await fetch('/settings/navidrome', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    nav_url: url,
                    nav_username: user,
                    nav_password: pass
                })
            });
            if (res.ok) {
                alert("✅ Navidrome 配置已保存！");
                urlInput.value = '';
                userInput.value = '';
                passInput.value = '';
                urlInput.placeholder = "*****已配置*****";
                userInput.placeholder = "*****已配置*****";
                passInput.placeholder = "*****已配置*****";
                if (user) {
                    userBadge.style.display = 'none'; 
                }
            } else {
                alert("❌ 保存失败");
            }
        } catch (e) {
            alert("❌ 保存出错: " + e.message);
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = "保存 Navidrome 配置";
        }
    });
}

function initDeleteButtons() {
    const delNavBtn = document.getElementById('del-nav-btn');
    if (delNavBtn) {
        delNavBtn.addEventListener('click', async () => {
            if (!confirm("⚠️ 确定要删除 Navidrome 的所有配置信息吗？\n⚠️ 此操作将清空服务器地址,用户名和密码")) return;

            try {
                const res = await fetch('/settings/navidrome', { method: 'DELETE' });
                
                if (res.ok) {
                    alert("✅ Navidrome 配置已清除");
                    document.getElementById('nav-url').value = '';
                    document.getElementById('nav-user').value = '';
                    document.getElementById('nav-pass').value = '';
                    document.getElementById('nav-url').placeholder = "域名/ip:端口";
                    document.getElementById('nav-user').placeholder = "用户名";
                    document.getElementById('nav-pass').placeholder = "密码";
                    document.getElementById('nav-user-badge').style.display = 'none';
                } else {
                    alert("❌ 删除失败: " + res.statusText);
                }
            } catch (e) {
                alert("❌ 请求错误: " + e.message);
            }
        });
    }

    const delTidalBtn = document.getElementById('del-tidal-btn');
    if (delTidalBtn) {
        delTidalBtn.addEventListener('click', async () => {
            if (!confirm("⚠️ 确定要删除 Tidal 授权信息吗？\n⚠️ 这将清除 Token 信息")) return;

            try {
                const res = await fetch('/settings/tidal', { method: 'DELETE' });

                if (res.ok) {
                    alert("✅ Tidal 配置已清除");
                    const statusText = document.getElementById('tidal-status');
                    if (statusText) {
                        statusText.innerHTML = '<span style="color: #888">当前状态: 已清除 (无 Token ) ⚠️</span>';
                    }
                } else {
                    alert("❌ 删除失败: " + res.statusText);
                }
            } catch (e) {
                alert("❌ 请求错误: " + e.message);
            }
        });
    }
}

async function initTidalAuth() {
    const startBtn = document.getElementById('tidal-start-btn');
    const refreshBtn = document.getElementById('tidal-refresh-btn');
    const checkBtn = document.getElementById('tidal-check-btn');
    const authDisplay = document.getElementById('auth-step-display');
    const authLink = document.getElementById('auth-link');
    const userCode = document.getElementById('auth-user-code');
    const statusText = document.getElementById('tidal-status');
    if (!startBtn) return;
    checkTidalStatus(statusText);
    startBtn.addEventListener('click', () => {
        startBtn.disabled = true;
        startBtn.textContent = "正在连接脚本...";
        statusText.style.display = 'flex';
        statusText.style.justifyContent = 'center';
        statusText.innerHTML = '<span style="color:#fff">正在启动登录进程...</span>';
        if (checkBtn) {
            checkBtn.parentElement.style.display = 'block';
            checkBtn.disabled = false;
        }
        authDisplay.style.display = 'none';
        authLink.href = "#";
        let extractedUrl = null; 
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/run-login`;
        const ws = new WebSocket(wsUrl);
        ws.onopen = () => {
            console.log("Login WS Connected");
            startBtn.textContent = "脚本运行中...";
        };
        ws.onmessage = (event) => {
            const msg = event.data;
            console.log("WS:", msg);
            const urlMatch = msg.match(/(https?:\/\/[^\s]+)/);
            if (urlMatch) {
                extractedUrl = urlMatch[1];
                authDisplay.style.display = 'block';
                authLink.href = extractedUrl;
                authLink.textContent = extractedUrl;
            }
            const codeMatch = msg.match(/用户代码:\s*([A-Z0-9]+)/);
            if (codeMatch) {
                userCode.textContent = codeMatch[1];
                if (extractedUrl && extractedUrl.startsWith('http')) {
                    window.open(extractedUrl, '_blank');
                }
            }
            if (msg.includes("授权成功")) {
                const cleanMsg = msg.replace(/\[.*?\]\s*/, "");
                statusText.innerHTML = `<span style="color: #00bfa5">${cleanMsg}</span>`;
                setTimeout(() => { window.location.reload(); }, 1500);
                ws.close();
            } else if (msg.includes("Error") || msg.includes("Exception")) {
                statusText.innerHTML = `<span style="color: #ff4444">❌ ${msg}</span>`;
            }
        };
        ws.onclose = () => {
            startBtn.disabled = false;
            startBtn.textContent = "获取授权";
        };
        ws.onerror = (e) => {
            statusText.innerHTML = '<span style="color: #ff4444">❌ 连接 WebSocket 失败</span>';
            startBtn.disabled = false;
        };
    });
    checkBtn.addEventListener('click', async () => {
        await checkTidalStatus(statusText);
    });
    refreshBtn.addEventListener('click', async () => {
        if(!confirm("确定要调用后台脚本刷新 Token 吗？")) return;
        refreshBtn.disabled = true;
        refreshBtn.textContent = "执行脚本中...";
        try {
            const res = await fetch('/tidal/auth/refresh', { method: 'POST' });
            const data = await res.json();
            if (res.ok) {
                alert("✅ 脚本执行成功:\n" + data.message);
                checkTidalStatus(statusText);
            } else {
                if (res.status === 401) {
                    if(confirm("❌ 刷新失败: Refresh Token 可能已失效，\n" + (data.detail || "") + "\n\n是否重新开始授权？")) {
                        startBtn.click();
                    }
                } else {
                    alert("❌ 刷新错误:\n" + (data.detail || "未知错误"));
                }
            }
        } catch (e) {
            alert("❌ 网络请求失败: " + e.message);
        } finally {
            refreshBtn.disabled = false;
            refreshBtn.textContent = "刷新 Token";
        }
    });
}

async function checkTidalStatus(statusEl) {
    try {
        const res = await fetch('/get-token-region');
        if (res.ok) {
            const data = await res.json();
            if (data.region) {
                const regionCode = (data.region || "").replace(/[\r\n\s]+/g, '').trim();
                statusEl.style.display = 'block'; 
                statusEl.style.textAlign = 'center';
                statusEl.style.whiteSpace = 'nowrap';
                statusEl.innerHTML = `
                    <span style="display: inline-block;">
                        <span style="color: #fff; font-weight: bold;">当前账号区域:</span>
                        <span style="color: #ffeb3b; font-weight: bold; margin-left: 5px;">${regionCode}</span>
                    </span>
                `;
            } else {
                statusEl.innerHTML = '<span style="color: #888">当前状态: 无 Token ⚠️</span>';
            }
        }
    } catch (e) {
        console.error("Status check failed", e);
        statusEl.innerHTML = '<span style="color: #ff4444">获取状态失败</span>';
    }
}