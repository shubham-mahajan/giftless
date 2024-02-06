"""Basic Streaming Transfer Adapter

This transfer adapter offers 'basic' transfers by streaming uploads / downloads
through the Git LFS HTTP server. It can use different storage backends (local,
cloud, ...). This module defines an
interface through which additional streaming backends can be implemented.
"""

import posixpath
from typing import Any, Dict, Optional

from flask import Response, request, url_for
from flask_classful import route
from webargs.flaskparser import parser  # type: ignore

from giftless.auth.identity import Permission
from giftless.exc import InvalidPayload, NotFound
from giftless.storage import ExternalStorage, exc
from giftless.schema import ObjectSchema
from giftless.storage import StreamingStorage, VerifiableStorage
from giftless.transfer import PreAuthorizingTransferAdapter, ViewProvider
from giftless.util import add_query_params, get_callable, safe_filename
from giftless.view import BaseView


class VerifyView(BaseView):
    """Verify an object

    This view is actually not basic_streaming specific, and is used by other
    transfer adapters that need a 'verify' action as well.
    """

    route_base = '<organization>/<repo>/objects/storage'

    def __init__(self, storage: VerifiableStorage):
        self.storage = storage

    @route('/verify', methods=['POST'])
    def verify(self, organization, repo):
        schema = ObjectSchema()
        payload = parser.parse(schema)

        self._check_authorization(organization, repo, Permission.READ_META, oid=payload['oid'])

        prefix = posixpath.join(organization, repo)
        if not self.storage.verify_object(prefix, payload['oid'], payload['size']):
            raise InvalidPayload("Object does not exist or size does not match")
        return Response(status=200)

    @classmethod
    def get_verify_url(cls, organization: str, repo: str, oid: Optional[str] = None) -> str:
        """Get the URL for upload / download requests for this object
        """
        op_name = f'{cls.__name__}:verify'
        url: str = url_for(op_name, organization=organization, repo=repo, oid=oid, _external=True)
        return url


class ObjectsView(BaseView):

    route_base = '<organization>/<repo>/objects/storage'

    def __init__(self, storage: StreamingStorage):
        self.storage = storage

    def put(self, organization, repo, oid):
        """Upload a file to local storage

        For now, I am not sure this actually streams chunked uploads without reading the entire
        content into memory. It seems that in order to support this, we will need to dive deeper
        into the WSGI Server -> Werkzeug -> Flask stack, and it may also depend on specific WSGI
        server implementation and even how a proxy (e.g. nginx) is configured.
        """
        self._check_authorization(organization, repo, Permission.WRITE, oid=oid)
        stream = request.stream
        self.storage.put(prefix=f'{organization}/{repo}', oid=oid, data_stream=stream)
        return Response(status=200)

    def get(self, organization, repo, oid):
        """Get an file open file stream from local storage
        """
        self._check_authorization(organization, repo, Permission.READ, oid=oid)
        path = posixpath.join(organization, repo)

        filename = request.args.get('filename')
        filename = safe_filename(filename) if filename else None
        disposition = request.args.get('disposition')

        headers = {}
        if filename and disposition:
            headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
        elif disposition:
            headers = {'Content-Disposition': disposition}

        if self.storage.exists(path, oid):
            file = self.storage.get(path, oid)
            mime_type = self.storage.get_mime_type(path, oid)
            headers['Content-Type'] = mime_type
            return Response(file, direct_passthrough=True, status=200, headers=headers)
        else:
            raise NotFound("The object was not found")

    @classmethod
    def get_storage_url(cls, operation: str, organization: str, repo: str, oid: Optional[str] = None) -> str:
        """Get the URL for upload / download requests for this object
        """
        op_name = f'{cls.__name__}:{operation}'
        url: str = url_for(op_name, organization=organization, repo=repo, oid=oid, _external=True)
        return url


class BasicStreamingTransferAdapter(PreAuthorizingTransferAdapter, ViewProvider):

    def __init__(self, storage: StreamingStorage, action_lifetime: int):
        self.storage = storage
        self.action_lifetime = action_lifetime

    def upload(self, organization: str, repo: str, oid: str, size: int, extra: Optional[Dict[str, Any]] = None) -> Dict:
        response = {"oid": oid,
                    "size": size}

        prefix = posixpath.join(organization, repo)
        if not self.storage.exists(prefix, oid) or self.storage.get_size(prefix, oid) != size:
            response['actions'] = {
                "upload": {
                    "href": ObjectsView.get_storage_url('put', organization, repo, oid),
                    "header": self._preauth_headers(organization, repo, actions={'write'}, oid=oid),
                    "expires_in": self.action_lifetime
                },
                "verify": {
                    "href": VerifyView.get_verify_url(organization, repo),
                    "header": self._preauth_headers(organization, repo, actions={'verify'}, oid=oid,
                                                    lifetime=self.VERIFY_LIFETIME),
                    "expires_in": self.VERIFY_LIFETIME
                }
            }
            response['authenticated'] = True

        return response
    

    def download(self, organization: str, repo: str, oid: str, size: int,
                 extra: Optional[Dict[str, Any]] = None) -> Dict:
        prefix = posixpath.join(organization, repo)
        response = {"oid": oid,
                    "size": size}

        try:
            self._check_object(prefix, oid, size)
            response.update(self.storage.get_download_action(prefix, oid, size, self.action_lifetime, extra))
        except exc.StorageError as e:
            response['error'] = e.as_dict()

        if response.get('actions', {}).get('download'):  # type: ignore
            response['authenticated'] = True

        return response

    def register_views(self, app):
        ObjectsView.register(app, init_argument=self.storage)
        VerifyView.register(app, init_argument=self.storage)
    
    def _check_object(self, prefix: str, oid: str, size: int):
        """Raise specific domain error if object is not valid

        NOTE: this does not use storage.verify_object directly because
        we want ObjectNotFound errors to be propagated if raised
        """
        if self.storage.get_size(prefix, oid) != size:
            raise exc.InvalidObject('Object size does not match')


def factory(storage_class, storage_options, action_lifetime):
    """Factory for basic transfer adapter with local storage
    """
    storage = get_callable(storage_class, __name__)
    return BasicStreamingTransferAdapter(storage(**storage_options), action_lifetime)
