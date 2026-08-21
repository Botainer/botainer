/* botainer Track B gateway page logic.
 *
 * Loaded as an ES module by vnc.html, served ONLY by the local trusted broker
 * (never by the container). Imports the vendored, pinned noVNC RFB client and
 * connects back to the SAME origin's /websockify endpoint, so the strict CSP
 * (`connect-src ws://127.0.0.1:<port>`) matches by construction.
 *
 * Inputs (all supplied by `botainer plugin browser gateway` when it opens the
 * URL — never typed by the user):
 *   ?path=websockify%3Ftoken%3D<T>   ws path; the ~192-bit session token rides
 *                                    it, and the broker + websockify TokenFile
 *                                    BOTH reject a wrong/absent token.
 *   ?resize=scale                    scale the remote screen into the window.
 *   #password=<8ch>                  the per-session x11vnc RFB password. In the
 *                                    FRAGMENT deliberately: fragments are never
 *                                    sent in HTTP requests, so it can't appear
 *                                    in request lines or logs. The throwaway
 *                                    profile is deleted after the session.
 *
 * CLIPBOARD IS DELIBERATELY NOT WIRED (B4: default none / "type, don't
 * paste"). The RFB 'clipboard' event (ServerCutText, container → host — the
 * exfiltration/pastejacking direction) is ignored, and nothing here reads the
 * user's clipboard. One-way paste-in, if ever added, belongs in a
 * framing-aware proxy at the trusted boundary, not in this page.
 */
import RFB from './novnc/core/rfb.js';

const query = new URLSearchParams(window.location.search);
const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));

const wsPath = query.get('path') || 'websockify';
const password = fragment.get('password') || '';

const statusBar = document.getElementById('status');
const screen = document.getElementById('screen');

function setStatus(text, cls) {
    statusBar.textContent = text;
    statusBar.className = cls;
}

// Same-origin ws URL — location.host is 127.0.0.1:<broker-port>, matching the
// CSP connect-src exactly. The token stays inside wsPath.
const wsUrl = `ws://${window.location.host}/${wsPath}`;

const rfb = new RFB(screen, wsUrl, { credentials: { password: password } });
rfb.scaleViewport = query.get('resize') === 'scale';

rfb.addEventListener('connect', () => {
    setStatus(
        'connected — this is the AGENT’s browser (untrusted content); ' +
        'the page you are on is served locally by botainer',
        'connected');
});

rfb.addEventListener('disconnect', (e) => {
    if (e.detail && e.detail.clean) {
        setStatus('disconnected — re-run `botainer plugin browser gateway` to reconnect',
                  'failed');
    } else {
        setStatus('connection FAILED — is the session running? Re-run ' +
                  '`botainer plugin browser gateway` (it health-checks each hop)',
                  'failed');
    }
});

rfb.addEventListener('credentialsrequired', () => {
    // The gateway always supplies the password via the fragment; landing here
    // means the URL was truncated or hand-edited. Fail loud, don't prompt —
    // a hand-typed password path is exactly what Track B removes.
    setStatus('missing viewer password — open the exact URL that ' +
              '`botainer plugin browser gateway` printed', 'failed');
    rfb.disconnect();
});

rfb.addEventListener('securityfailure', (e) => {
    const reason = (e.detail && e.detail.reason) ? e.detail.reason : 'unknown';
    setStatus(`VNC security failure: ${reason} — stale session? Re-run ` +
              '`botainer plugin browser gateway`', 'failed');
});
