#!/bin/bash
# -*- coding: utf-8 -*-

rm Dockerfile
cp Dockerfile_CELERY_WORKER Dockerfile

fly deploy -c fly_celery_worker.toml
