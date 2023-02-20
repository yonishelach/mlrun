# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import json
from typing import Dict, List, Optional, Tuple

import mlrun.errors
import mlrun.utils.singleton
from mlrun.api.schemas.marketplace import (
    AssetKind,
    MarketplaceCatalog,
    MarketplaceItem,
    MarketplaceItemMetadata,
    MarketplaceItemSpec,
    MarketplaceSource,
    ObjectStatus,
)
from mlrun.api.utils.singletons.k8s import get_k8s
from mlrun.config import config
from mlrun.datastore import store_manager
from mlrun.utils import logger

from ..schemas import SecretProviderName
from .secrets import Secrets, SecretsClientType

# Using a complex separator, as it's less likely someone will use it in a real secret name
secret_name_separator = "-__-"


class Marketplace(metaclass=mlrun.utils.singleton.Singleton):
    def __init__(self):
        self._internal_project_name = config.marketplace.k8s_secrets_project_name
        self._catalogs = {}

    @staticmethod
    def _in_k8s():
        k8s_helper = get_k8s()
        return (
            k8s_helper is not None and k8s_helper.is_running_inside_kubernetes_cluster()
        )

    @staticmethod
    def _generate_credentials_secret_key(source, key=""):
        full_key = source + secret_name_separator + key
        return Secrets().generate_client_project_secret_key(
            SecretsClientType.marketplace, full_key
        )

    def add_source(self, source: MarketplaceSource):
        source_name = source.metadata.name
        credentials = source.spec.credentials
        if credentials:
            self._store_source_credentials(source_name, credentials)

    def remove_source(self, source_name):
        self._catalogs.pop(source_name, None)
        if not self._in_k8s():
            return

        source_credentials = self._get_source_credentials(source_name)
        if not source_credentials:
            return
        secrets_to_delete = [
            self._generate_credentials_secret_key(source_name, key)
            for key in source_credentials
        ]
        Secrets().delete_project_secrets(
            self._internal_project_name,
            SecretProviderName.kubernetes,
            secrets_to_delete,
            allow_internal_secrets=True,
        )

    def _store_source_credentials(self, source_name, credentials: dict):
        if not self._in_k8s():
            raise mlrun.errors.MLRunInvalidArgumentError(
                "MLRun is not configured with k8s, marketplace source credentials cannot be stored securely"
            )

        adjusted_credentials = {
            self._generate_credentials_secret_key(source_name, key): value
            for key, value in credentials.items()
        }
        Secrets().store_project_secrets(
            self._internal_project_name,
            mlrun.api.schemas.SecretsData(
                provider=SecretProviderName.kubernetes, secrets=adjusted_credentials
            ),
            allow_internal_secrets=True,
        )

    def _get_source_credentials(self, source_name):
        if not self._in_k8s():
            return {}

        secret_prefix = self._generate_credentials_secret_key(source_name)
        secrets = (
            Secrets()
            .list_project_secrets(
                self._internal_project_name,
                SecretProviderName.kubernetes,
                allow_secrets_from_k8s=True,
                allow_internal_secrets=True,
            )
            .secrets
        )

        source_secrets = {}
        for key, value in secrets.items():
            if key.startswith(secret_prefix):
                source_secrets[key[len(secret_prefix) :]] = value

        return source_secrets

    def _transform_catalog_dict_to_schema(
        self, source: MarketplaceSource, catalog_dict
    ):
        catalog = MarketplaceCatalog(catalog=[], channel=source.spec.channel)
        # Loop over objects, then over object versions.
        for object_name in catalog_dict:
            object_dict = catalog_dict[object_name]
            for version_tag in object_dict:
                version_dict = object_dict[version_tag]
                object_details_dict = version_dict.copy()
                spec_dict = object_details_dict.pop("spec", None)
                metadata = MarketplaceItemMetadata(
                    tag=version_tag, **object_details_dict
                )
                item_uri = source.get_full_uri(metadata.get_relative_path())
                spec = MarketplaceItemSpec(item_uri=item_uri, **spec_dict)
                assets = self._prepare_asset_paths(
                    spec, metadata, source.spec.object_type
                )
                item = MarketplaceItem(
                    metadata=metadata,
                    spec=spec,
                    status=ObjectStatus(),
                    assets=assets,
                )
                catalog.catalog.append(item)

        return catalog

    @staticmethod
    def _prepare_asset_paths(
        item_spec: MarketplaceItemSpec,
        item_metadata: MarketplaceItemMetadata,
        source_type: str,
    ) -> Dict[str, str]:
        """
        Prepares a dictionary with assets path (relative to catalog location) in advance for get_asset function.

        :param item_spec:       item spec object, which may contain information on assets
        :param item_metadata:   item metadata object, which may contain information on assets
        :param source_type:     type of the marketplace source, each marketplace source contain different assets.

        :return: dictionary with asset names as keys and relative asset paths as values
        """
        assets_paths = {}
        assets_dict = AssetKind[source_type].value
        item_path = item_metadata.get_relative_path()
        # Iterating over the possible assets and adds the
        for asset, asset_location in assets_dict.items():
            if "/" in asset_location:
                assets_paths[asset] = item_path + asset_location
            else:
                asset_part, asset_location = asset_location.split(":")
                asset_part = item_spec if asset_part == "spec" else item_metadata
                relative_path = getattr(asset_part, asset_location, None)
                if not relative_path:
                    logger.warn(f"asset {asset} not found in item {item_metadata.name}")
                else:
                    assets_paths[asset] = item_path + relative_path

        return assets_paths

    def get_source_catalog(
        self,
        source: MarketplaceSource,
        version=None,
        tag=None,
        force_refresh=False,
    ) -> MarketplaceCatalog:
        source_name = source.metadata.name
        if not self._catalogs.get(source_name) or force_refresh:
            url = source.get_catalog_uri()
            credentials = self._get_source_credentials(source_name)
            catalog_data = mlrun.run.get_object(url=url, secrets=credentials)
            catalog_dict = json.loads(catalog_data)
            catalog = self._transform_catalog_dict_to_schema(source, catalog_dict)
            self._catalogs[source_name] = catalog
        else:
            catalog = self._catalogs[source_name]

        result_catalog = MarketplaceCatalog(catalog=[], channel=source.spec.channel)
        for item in catalog.catalog:
            if (tag is None or item.metadata.tag == tag) and (
                version is None or item.metadata.version == version
            ):
                result_catalog.catalog.append(item)

        return result_catalog

    def get_item(
        self,
        source: MarketplaceSource,
        item_name,
        version=None,
        tag=None,
        force_refresh=False,
    ) -> MarketplaceItem:
        catalog = self.get_source_catalog(source, version, tag, force_refresh)
        return self._get_item_from_catalog(catalog.catalog, item_name, version, tag)

    def get_item_object_using_source_credentials(self, source: MarketplaceSource, url):
        credentials = self._get_source_credentials(source.metadata.name)

        if not url.startswith(source.spec.path):
            raise mlrun.errors.MLRunInvalidArgumentError(
                "URL to retrieve must be located in the source filesystem tree"
            )

        if url.endswith("/"):
            obj = store_manager.object(url=url, secrets=credentials)
            listdir = obj.listdir()
            return {
                "listdir": listdir,
            }
        else:
            catalog_data = mlrun.run.get_object(url=url, secrets=credentials)
        return catalog_data

    def get_asset(
        self,
        source: MarketplaceSource,
        item_name: str,
        asset_name: str,
        tag: Optional[str] = None,
        version: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Retrieve asset object from marketplace source.

        :param source:      marketplace source
        :param item_name:   item name
        :param asset_name:  asset name, like source, example, etc
        :param tag:         latest or version, default to latest
        :param version:     indicates the version of the item

        :return: tuple of asset as bytes and url of asset
        """
        tag = version or tag
        credentials = self._get_source_credentials(source.metadata.name)
        catalog = self.get_source_catalog(source=source, tag=tag, version=version)

        # catalog is already filtered by tag and version, so need  to filter only by item:
        item = self._get_item_from_catalog(catalog.catalog, item_name, version, tag)

        asset_relative_path = item.assets.get(asset_name, None)
        if not asset_relative_path:
            raise mlrun.errors.MLRunNotFoundError(
                f"asset {asset_name} of source type {source.spec.object_type} not found"
            )
        asset_full_path = source.get_full_uri(asset_relative_path)
        return (
            mlrun.run.get_object(url=asset_full_path, secrets=credentials),
            asset_full_path,
        )

    @staticmethod
    def _get_item_from_catalog(
        catalog: List[MarketplaceItem], item_name, version, tag
    ) -> MarketplaceItem:
        """
        Retrieve item from catalog, assuming that the catalog is already filtered by tags and versions.
        Raise errors if the number of collected items from catalog is not exactly one.
        Use when expected to get exactly one item.

        :param catalog:     list of items
        :param item_name:   item name
        :param version:     item version
        :param tag:         item tag
        :return:   item object from catalog
        """
        items = [item for item in catalog if item.metadata.name == item_name]
        if not items:
            raise mlrun.errors.MLRunNotFoundError(
                f"Item not found. source={item_name}, version={version}"
            )
        if len(items) > 1:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Query resulted in more than 1 catalog items. "
                + f"source={item_name}, version={version}, tag={tag}"
            )
        return items[0]
