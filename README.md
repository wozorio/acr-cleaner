# ACR Cleaner

[![GitHub](https://img.shields.io/github/license/wozorio/acr-cleaner)](https://github.com/wozorio/acr-cleaner/blob/main/LICENSE)
[![CI](https://github.com/wozorio/acr-cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/wozorio/acr-cleaner/actions/workflows/ci.yml)

## Description

A script to clean up an Azure container registry by deleting dangling images and images which are older than a specified period of time (in days) if they are not being used.
It was tested and validated against container registries with single-architecture images.

### Built With

Python 3.11.2

## Getting Started

### Prerequisites

1. Set environment variables:

   ```bash
   export AZURE_CLIENT_ID=<AZURE_CLIENT_ID>
   export AZURE_CLIENT_SECRET=<AZURE_CLIENT_SECRET>
   export AZURE_TENANT_ID=<AZURE_TENANT_ID>
   export AZURE_SUBSCRIPTION_ID=<AZURE_SUBSCRIPTION_ID>
   ```

1. Install requirements:

   ```bash
   pip install poetry
   poetry install --without dev
   ```

1. Get a list of images in use:
   ```bash
   kubectl get pods \
     --all-namespaces \
     --output jsonpath='{range .items[*]} {range .status.containerStatuses[*]}{.imageID}{"\n"}{end}' \
   | grep <REGISTRY_NAME> \
   | uniq >deployed_images.txt
   ```

### Usage

```bash
Usage: poetry run acr_cleaner.py REGISTRY_NAME REGISTRY_RESOURCE_GROUP MAX_IMAGE_AGE DEPLOYED_IMAGES

Arguments:
    REGISTRY_NAME              The name of the container registry
    REGISTRY_RESOURCE_GROUP    The resource group where the container registry is deployed
    MAX_IMAGE_AGE              The max age (in days) an image can have
    DEPLOYED_IMAGES            A comma-separated list of images that are currently deployed (in use). These will be handled as exceptions
                               and therefore they will not be deleted even if they are older than the `MAX_IMAGE_AGE` argument
```
