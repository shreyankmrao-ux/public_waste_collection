document.querySelectorAll('.password-toggle').forEach((toggle) => {
    toggle.addEventListener('click', () => {
        const input = document.getElementById(toggle.getAttribute('aria-controls'));
        if (!input) return;
        const showPassword = input.type === 'password';
        input.type = showPassword ? 'text' : 'password';
        toggle.textContent = showPassword ? 'Hide' : 'Show';
        toggle.setAttribute('aria-pressed', String(showPassword));
        toggle.setAttribute('aria-label', `${showPassword ? 'Hide' : 'Show'} ${input.labels[0]?.textContent || 'password'}`);
    });
});
