# Vendored browser assets

These files are pinned runtime browser assets used by `static/index.html`.

- `xterm@5.3.0`
- `xterm-addon-fit@0.8.0`
- `xterm-addon-unicode11@0.4.0`
- `xterm-addon-webgl@0.16.0`

Each package is distributed under the MIT license. The corresponding `LICENSE`
file is included in each package directory.

`xterm-addon-webgl@0.16.0` has one local platform-detection fix: native Safari
is identified by its `Version/<major> ... Safari` token instead of any user
agent containing `Safari`. Chrome and Vivaldi on iOS replace the `Version`
token with `CriOS`, and the upstream check otherwise misclassifies them as
Safari 0 and disables WebGL.
