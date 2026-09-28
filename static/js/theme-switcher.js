// Theme switcher for Attendancify with Spotify-inspired themes
document.addEventListener('DOMContentLoaded', function() {
    const themeToggle = document.getElementById('theme-toggle');

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        document.body.setAttribute('data-theme', theme);
    }

    if (themeToggle) {
        // Check for saved theme or default to dark
        const savedTheme = localStorage.getItem('theme') || 'dark';
        applyTheme(savedTheme);

        // Update toggle button icon based on current theme
        updateToggleIcon(savedTheme);

        // Add click event listener
        themeToggle.addEventListener('click', function() {
            const currentTheme = document.documentElement.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';

            // Apply new theme
            applyTheme(newTheme);

            // Update toggle button icon
            updateToggleIcon(newTheme);

            // Save preference
            localStorage.setItem('theme', newTheme);

            // Dispatch custom event for other components to react
            document.dispatchEvent(new CustomEvent('themeChanged', { detail: newTheme }));
        });
    }

    function updateToggleIcon(theme) {
        const icon = themeToggle.querySelector('i');
        if (icon) {
            icon.classList.remove('fa-sun', 'fa-moon');
            icon.classList.add(theme === 'dark' ? 'fa-moon' : 'fa-sun');
        }
    }

    // Listen for theme changes from other sources
    document.addEventListener('themeChanged', function(e) {
        const newTheme = e.detail;
        applyTheme(newTheme);
        updateToggleIcon(newTheme);
        localStorage.setItem('theme', newTheme);
    });
});
