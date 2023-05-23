# ACR Cleaner

A script to clean up an Azure container registry by deleting dangling images and images 
which are older than a specified period of time (in days) if they are not being used.

## Prerequisites

1. Ensure the following environment variables are set:
    ```bash
    $ export AZURE_CLIENT_ID=<AZURE_CLIENT_ID>
    $ export AZURE_CLIENT_SECRET=<AZURE_CLIENT_SECRET>
    $ export AZURE_TENANT_ID=<AZURE_TENANT_ID>
    $ export AZURE_SUBSCRIPTION_ID=<AZURE_SUBSCRIPTION_ID>
    ```

1. Install requirements:

    ```bash
    $ pip install -r requirements.txt
    ```

## Usage

```bash
Usage: acr_cleaner.py REGISTRY_NAME REGISTRY_RESOURCE_GROUP MAX_IMAGE_AGE DEPLOYED_IMAGES
```
