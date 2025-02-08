#!/bin/bash
# -*- coding: utf-8 -*-

rm Dockerfile
cp Dockerfile_MAIN_API Dockerfile

fly deploy -c fly_main_api.toml
