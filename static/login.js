(function() {
    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        const response = await originalFetch(...args);
        if (response.status === 401 && typeof args[0] === 'string' && !args[0].includes('/login') && !args[0].includes('/check-auth')) {
            const loginModal = document.getElementById('login-modal');
            if (loginModal && !loginModal.classList.contains('is-visible')) { 
                console.warn('会话失效或未授权 (401)。正在显示登录窗口...');
                loginModal.classList.add('is-visible');
                document.body.classList.add('modal-open');
                return new Promise(() => {});
            }
        }
        return response;
    };
})();

(async () => {
    try {
        const response = await fetch('/check-auth');
        if (response.ok) {
            document.getElementById('login-modal').classList.remove('is-visible');
            document.getElementById('main-container').style.display = 'block';
            document.body.classList.remove('modal-open');
        } else {
            document.getElementById('login-modal').classList.add('is-visible');
            document.body.classList.add('modal-open');
        }
    } catch (error) {
        console.error('认证检查失败:', error);
        const loginError = document.getElementById('login-error');
        if (loginError) {
            loginError.textContent = '无法连接到认证服务器';
        }
        document.getElementById('login-modal').classList.add('is-visible');
        document.body.classList.add('modal-open');
    }
})();

document.addEventListener('DOMContentLoaded', () => {
    const loginModal = document.getElementById('login-modal');
    const registerModal = document.getElementById('register-modal');
    
    const loginForm = document.getElementById('login-form');
    const registerForm = document.getElementById('register-form');
    
    const loginButton = document.getElementById('login-button');
    const registerButton = document.getElementById('register-button');
    
    const loginError = document.getElementById('login-error');
    const registerError = document.getElementById('register-error');
    
    const showRegisterBtn = document.getElementById('show-register-btn');
    const showLoginBtn = document.getElementById('show-login-btn');

    if (showRegisterBtn) {
        showRegisterBtn.addEventListener('click', () => {
            loginModal.classList.remove('is-visible');
            registerModal.classList.add('is-visible');
            document.getElementById('reg-username').value = '';
            document.getElementById('reg-password').value = '';
            document.getElementById('reg-invite-code').value = '';
            registerError.textContent = '';
        });
    }

    if (showLoginBtn) {
        showLoginBtn.addEventListener('click', () => {
            registerModal.classList.remove('is-visible');
            loginModal.classList.add('is-visible');
            loginError.textContent = '';
        });
    }

    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;

            loginButton.disabled = true;
            loginButton.textContent = '登录中...';
            loginError.textContent = '';

            try {
                const response = await fetch('/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password }),
                });

                if (response.ok) {
                    location.reload();
                } else {
                    const errorData = await response.json();
                    loginError.textContent = errorData.detail || '用户名或密码无效';
                    loginModal.classList.add('shake');
                    setTimeout(() => loginModal.classList.remove('shake'), 500);
                }
            } catch (error) {
                loginError.textContent = '无法连接到服务器';
            } finally {
                loginButton.disabled = false;
                loginButton.textContent = '登录';
            }
        });
    }

    if (registerForm) {
        registerForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('reg-username').value;
            const password = document.getElementById('reg-password').value;
            const inviteCode = document.getElementById('reg-invite-code').value;

            registerButton.disabled = true;
            registerButton.textContent = '注册中...';
            registerError.textContent = '';

            try {
                const response = await fetch('/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        username: username, 
                        password: password,
                        invite_code: inviteCode
                    }),
                });

                if (response.ok) {
                    alert('注册成功！正在自动登录...');
                    
                    const loginResp = await fetch('/login', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ username, password }),
                    });
                    
                    if (loginResp.ok) {
                        location.reload();
                    } else {
                        registerModal.classList.remove('is-visible');
                        loginModal.classList.add('is-visible');
                    }
                } else {
                    const errorData = await response.json();
                    registerError.textContent = errorData.detail || '注册失败';
                }
            } catch (error) {
                registerError.textContent = '无法连接到服务器';
            } finally {
                registerButton.disabled = false;
                registerButton.textContent = '注册';
            }
        });
    }
});