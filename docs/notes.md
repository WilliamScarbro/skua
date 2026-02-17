<!-- SPDX-License-Identifier: BUSL-1.1 -->


# git clone issues

* emits warning about missing directory mount after add
* clones onto local machine and then runs image with mounted directory, what about cloning into image directly? Takes a long time for large repos, but doesn't rely on host machine storage (if docker is not being run locally)
Intermediate soluton -> create PV which docker container mounts
* Directory column in `skua list` should be "SOURCE"


# todo
* functionality to edit project configuration (requires status:stopped)
* reuse local agent keys (doesn't require login in container)
* supply prompt and run headless (with monitoring)
* store prebuilt skua images in dockerhub
* adaptive images (project dependent) add image to `skua list` when image is not agent base image, separate list columns into important (default) and full (skua list --verbose)
