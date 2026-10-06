# Leaflet runtime assets

This directory contains the unmodified browser distribution of Leaflet 1.9.4.
It is served from the Agrisky AI API origin so immutable evidence maps do not
depend on a third-party CDN at review time.

- Project: https://leafletjs.com/
- Version: 1.9.4
- License: BSD-2-Clause (see `LICENSE`)

The evidence HTML remains an immutable artifact. Agrisky AI creates a separate
runtime response that replaces only the Leaflet asset URLs and applies a
restrictive Content Security Policy.
