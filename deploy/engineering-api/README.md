# Engineering API image recipe

This is an API-only candidate recipe for the Engineering source overlay. It is
separate from `deploy/contabo`, whose API/bridge/native workflow remains
unchanged. The source is `564be49ec362b574c873a30b33c4c5265dedb426`, validated
by CI run `41476580052` on `codex/ingenieria-source-20261102`.

The runtime context is generated only from `runtime-allowlist.json`. It keeps
the existing assistant, Avance and production modules from the reviewed base
and adds the four Engineering overlay files. Tests, fixtures, secrets,
bridge files and native-UI files are excluded. The workflow builds, smoke
checks, layer-verifies and archives only the API image, then publishes only
that tested image behind the existing `scm-pilot-image-public` environment.

No deployment is performed by this recipe. The protected workflow branch,
permissions, reviewer gate and immutable `github.sha` recipe checkout must
remain unchanged.
