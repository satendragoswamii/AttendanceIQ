/* ========================================================================
   APP BACKGROUND - shared animated backdrop for every page
   Mirrors the signed-out landing page look (tsParticles + radial glows +
   mouse parallax) so the whole app shares one background. Replaces the old
   canvas particle system (particles-bg.js).
   ======================================================================== */

/* Glow that follows the cursor. Sits above the page (pointer-events: none) and
   lights up whatever is underneath. Mouse-driven pointers only -- no glow on
   touch screens, where there is no cursor to follow. */
function initCursorGlow() {
    var effect = document.body.dataset.cursorEffect || 'both';
    if (effect !== 'glow' && effect !== 'both') return;
    if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
    if (document.getElementById('cursor-glow')) return;

    // .cg-lens brightens whatever sits under the glow (text, icons, cards);
    // .cg-tint paints the coloured light on top of it.
    var glow = document.createElement('div');
    glow.id = 'cursor-glow';
    glow.setAttribute('aria-hidden', 'true');
    glow.innerHTML = '<div class="cg-inner"><div class="cg-lens"></div><div class="cg-tint"></div></div>';
    document.body.appendChild(glow);

    var x = 0, y = 0, raf = null;

    function apply() {
        raf = null;
        glow.style.transform = 'translate3d(' + x + 'px, ' + y + 'px, 0)';
    }

    document.addEventListener('mousemove', function (e) {
        x = e.clientX;
        y = e.clientY;
        glow.classList.add('is-visible');
        if (!raf) raf = requestAnimationFrame(apply);
    });

    // Fade out when the pointer leaves the window entirely.
    document.addEventListener('mouseleave', function () {
        glow.classList.remove('is-visible');
    });
    document.addEventListener('mouseenter', function () {
        glow.classList.add('is-visible');
    });
}

document.addEventListener('DOMContentLoaded', function () {
    initCursorGlow();

    var bg = document.getElementById('particles-background');
    if (!bg) return;

    // Subtle mouse parallax on the background layer.
    document.addEventListener('mousemove', function (e) {
        var x = (e.clientX / window.innerWidth - 0.5) * 16;
        var y = (e.clientY / window.innerHeight - 0.5) * 16;
        bg.style.transform = 'translate(' + x + 'px, ' + y + 'px)';
    });

    // tsParticles: 70 particles, cyan/purple/pink, thin low-opacity connecting
    // lines, brisk floating movement. Kept lightweight (moderate count, no
    // hover interactivity) so it stays cheap on every page.
    if (window.tsParticles && document.getElementById('app-particles')) {
        tsParticles.load('app-particles', {
            fullScreen: { enable: false },
            background: { color: { value: 'transparent' } },
            fpsLimit: 60,
            particles: {
                number: { value: 70, density: { enable: true, area: 900 } },
                color: { value: ['#27C8FF', '#B54CFF', '#FF57C7'] },
                opacity: { value: 0.55 },
                size: { value: { min: 1, max: 3 } },
                links: {
                    enable: true,
                    distance: 140,
                    color: '#5B7BFF',
                    opacity: 0.15,
                    width: 1
                },
                move: {
                    enable: true,
                    speed: 1.3,
                    direction: 'none',
                    random: true,
                    straight: false,
                    outModes: { default: 'bounce' }
                }
            },
            interactivity: {
                events: { onHover: { enable: false }, onClick: { enable: false } }
            },
            detectRetina: true
        });
    }
});
