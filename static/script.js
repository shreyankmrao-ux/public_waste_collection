document.querySelectorAll('.password-toggle').forEach((toggle) => {
    const eye = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
    const eyeOff = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8"/><path d="M9.9 5.2A10.7 10.7 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-4.1 4.8M6.2 6.2C3.5 8 2 12 2 12s3.6 7 10 7a10 10 0 0 0 3-.5"/></svg>';
    const input = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!input) return;
    toggle.innerHTML = eye;
    toggle.setAttribute('aria-label', `Show ${input.labels[0]?.textContent || 'password'}`);
    toggle.addEventListener('click', () => {
        const showPassword = input.type === 'password';
        input.type = showPassword ? 'text' : 'password';
        toggle.innerHTML = showPassword ? eyeOff : eye;
        toggle.setAttribute('aria-pressed', String(showPassword));
        toggle.setAttribute('aria-label', `${showPassword ? 'Hide' : 'Show'} ${input.labels[0]?.textContent || 'password'}`);
    });
});
