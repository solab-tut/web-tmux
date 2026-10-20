# Vendored browser assets

These files are pinned runtime browser assets used by `static/index.html`.

- `xterm@5.3.0`
- `xterm-addon-fit@0.8.0`
- `xterm-addon-unicode11@0.4.0`
- `xterm-addon-webgl@0.16.0`
- `markdown-it@14.1.0` (`dist/markdown-it.min.js`, the browser bundle as
  published to npm)

Each package is distributed under the MIT license. The corresponding `LICENSE`
file is included in each package directory. The markdown-it bundle also
contains its dependencies (`entities@4.5.0` under BSD-2-Clause; `mdurl@2.0.0`,
`uc.micro@2.1.0`, `linkify-it@5.0.0` and `punycode.js@2.3.1` under MIT); their
license texts are in `markdown-it/14.1.0/THIRD_PARTY_LICENSES`.

`xterm-addon-webgl@0.16.0` has one local platform-detection fix: native Safari
is identified by its `Version/<major> ... Safari` token instead of any user
agent containing `Safari`. Chrome and Vivaldi on iOS replace the `Version`
token with `CriOS`, and the upstream check otherwise misclassifies them as
Safari 0 and disables WebGL.
