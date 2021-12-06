import inspect
import uuid
from abc import ABCMeta, ABC
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Type,
    Union,
    cast,
    no_type_check, Iterator, Tuple, overload,
)

from django.db.models import Model, QuerySet
from django.http import HttpResponse
from injector import inject, is_decorated_with_inject
from ninja import NinjaAPI
from ninja.constants import NOT_SET
from ninja.operation import Operation
from ninja.security.base import AuthBase
from ninja.types import DictStrAny
from ninja.utils import normalize_path

from ninja_extra.exceptions import APIException, NotFound, PermissionDenied, bad_request
from ninja_extra.operation import PathView
from ninja_extra.permissions import AllowAny, BasePermission
from ninja_extra.shortcuts import (
    fail_silently,
    get_object_or_exception,
    get_object_or_none,
)
from ninja_extra.types import PermissionType

from django.urls import URLPattern, path as django_path
from .response import Detail, Id, Ok
from .route.route_functions import RouteFunction
from .registry import ControllerRegistry

if TYPE_CHECKING:
    from .route.context import RouteContext  # pragma: no cover
    from ninja_extra import NinjaExtraAPI   # pragma: no cover


class MissingAPIControllerDecoratorException(Exception):
    pass


def get_route_functions(cls) -> Iterable[RouteFunction]:
    for method in cls.__dict__.values():
        if isinstance(method, RouteFunction):
            yield method


def compute_api_route_function(
    base_cls: Type, api_controller_instance: "APIController"
) -> None:
    for cls_route_function in get_route_functions(base_cls):
        cls_route_function.api_controller = api_controller_instance
        api_controller_instance.add_operation_from_route_function(cls_route_function)


# class APIControllerModelMetaclass(ABCMeta):
#     @no_type_check
#     def __new__(mcs, name: str, bases: tuple, namespace: dict):
#         cls = super().__new__(mcs, name, bases, namespace)
#         if name == "APIController" and ABC in bases:
#             return cls
#
#         cls = cast(Type[APIController], cls)
#         cls._path_operations = {}
#         cls.api = namespace.get("api", None)
#         cls.registered = False
#
#         if not namespace.get("tags"):
#             tag = str(cls.__name__).lower().replace("controller", "")
#             cls.tags = [tag]
#
#         for base_cls in reversed(bases):
#             if base_cls is not APIController and issubclass(base_cls, APIController):
#                 compute_api_route_function(base_cls, cls)
#
#         compute_api_route_function(cls)
#         if not is_decorated_with_inject(cls.__init__):
#             fail_silently(inject, constructor_or_class=cls)
#         return cls

class ControllerBaseModelMetaclass(ABCMeta):
    @no_type_check
    def __new__(mcs, name: str, bases: tuple, namespace: dict):
        cls = super().__new__(mcs, name, bases, namespace)
        if name == "ControllerBase" and ABC in bases:
            return cls
        cls._api_controller = namespace.get('_api_controller', None)
        return cls


class APIController:
    # TODO: implement csrf on route function or on controller level. Which can override api csrf
    #   controller should have a csrf ON unless turned off by api instance
    controller_class: Optional[Type['ControllerBase']] = None

    def __init__(
        self,
        prefix: str,
        *,
        auth: Any = NOT_SET,
        tags: Union[Optional[List[str]], str] = None,
        permissions: Optional["PermissionType"] = None,
        auto_import: bool = True
    ) -> None:

        self.prefix = prefix
        # `auth` primarily defines APIController route function global authentication method.
        self.auth: Optional[AuthBase] = auth

        self.tags = tags

        self.auto_import = (
            auto_import  # set to false and it would be ignored when api.auto_discover is called
        )
        self.permission_classes = permissions or [AllowAny]
        self.controller_class = None
        # `_path_operations` a converted dict of APIController route function used by Django-Ninja library
        self._path_operations: Dict[str, PathView] = dict()
        # `permission_classes` primarily holds permission defined by the ControllerRouter and its used as
        # a fallback if route functions has no permissions definition
        self.permission_classes: PermissionType = permissions or [AllowAny]
        # `registered` prevents controllers from being register twice or exist in two different `api` instances
        self.registered: bool = False

    @property
    def tags(self) -> Optional[List[str]]:
        # `tags` is a property for grouping endpoint in Swagger API docs
        return self._tags

    @tags.setter
    def tags(self, value: Union[str, List[str], None]) -> None:
        tag: Optional[List[str]] = cast(Optional[List[str]], value)
        if tag and isinstance(value, str):
            tag = [value]
        self._tags = tag

    def __call__(self, cls: Type) -> Type["ControllerBase"]:
        self.auto_import = getattr(cls, 'auto_import', self.auto_import)
        if not issubclass(cls, ControllerBase):
            cls = type(cls.__name__, (ControllerBase, cls), {'_api_controller': self})
        else:
            cls._api_controller = self

        if not self.tags:
            tag = str(cls.__name__).lower().replace("controller", "")
            self.tags = [tag]

        bases = inspect.getmro(cls)
        for base_cls in bases:
            if base_cls not in [ControllerBase, ABC, object]:
                compute_api_route_function(base_cls, self)

        if not is_decorated_with_inject(cls.__init__):
            fail_silently(inject, constructor_or_class=cls)

        self.controller_class = cls
        ControllerRegistry().add_controller(cls)
        return cls

    @property
    def path_operations(self) -> DictStrAny:
        return self._path_operations

    def set_api_instance(self, api: "NinjaExtraAPI") -> None:
        self.controller_class.api = api
        for path_view in self.path_operations.values():
            path_view.set_api_instance(api, self)

    def build_routers(self) -> List[Tuple[str, "APIController"]]:
        return [(self.prefix, self)]

    def urls_paths(self, prefix: str) -> Iterator[URLPattern]:
        for path, path_view in self.path_operations.items():
            path = path.replace("{", "<").replace("}", ">")
            route = "/".join([i for i in (prefix, path) if i])
            # to skip lot of checks we simply treat double slash as a mistake:
            route = normalize_path(route)
            route = route.lstrip("/")
            for op in path_view.operations:
                yield django_path(
                    route, path_view.get_view(), name=cast(str, op.url_name)
                )

    def __repr__(self) -> str:
        return f"<controller - {self.controller_class.__name__}>"

    def __str__(self) -> str:
        return f"{self.controller_class.__name__}"

    def add_operation_from_route_function(self, route_function: RouteFunction) -> None:
        # converts route functions to Operation model
        route_function.route.route_params.operation_id = (
            f"{str(uuid.uuid4())[:8]}_controller_{route_function.route.view_func.__name__}"
        )
        self.add_api_operation(
            view_func=route_function.as_view, **route_function.route.route_params.dict()
        )

    def add_api_operation(
        self,
        path: str,
        methods: List[str],
        view_func: Callable,
        *,
        auth: Any = NOT_SET,
        response: Any = NOT_SET,
        operation_id: Optional[str] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        deprecated: Optional[bool] = None,
        by_alias: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        url_name: Optional[str] = None,
        include_in_schema: bool = True,
    ) -> Operation:
        if path not in self._path_operations:
            path_view = PathView()
            self._path_operations[path] = path_view
        else:
            path_view = self._path_operations[path]
        operation = path_view.add_operation(
            path=path,
            methods=methods,
            view_func=view_func,
            auth=auth or self.auth,
            response=response,
            operation_id=operation_id,
            summary=summary,
            description=description,
            tags=tags,
            deprecated=deprecated,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            url_name=url_name,
            include_in_schema=include_in_schema,
        )
        return operation


class ControllerBase(ABC, metaclass=ControllerBaseModelMetaclass):
    # `_api_controller` a reference to APIController instance
    _api_controller: Optional[APIController] = None

    # `api` a reference to NinjaExtraAPI on APIController registration
    api: Optional[NinjaAPI] = None

    # `context` variable will change based on the route function called on the APIController
    # that way we can get some specific items things that belong the route function during execution
    context: Optional["RouteContext"] = None

    Ok = Ok
    Id = Id
    Detail = Detail
    bad_request = bad_request

    @classmethod
    def get_api_controller(cls) -> APIController:
        if not cls._api_controller:
            raise MissingAPIControllerDecoratorException(
                "api_controller not found. "
                "Did you forget to use the `api_controller` decorator"
            )
        return cls._api_controller

    @classmethod
    def permission_denied(cls, permission: BasePermission) -> None:
        message = getattr(permission, "message", None)
        raise PermissionDenied(message)

    def get_object_or_exception(
        self,
        klass: Union[Type[Model], QuerySet],
        error_message: str = None,
        exception: Type[APIException] = NotFound,
        **kwargs: Any,
    ) -> Any:
        obj = get_object_or_exception(
            klass=klass, error_message=error_message, exception=exception, **kwargs
        )
        self.check_object_permissions(obj)
        return obj

    def get_object_or_none(
        self, klass: Union[Type[Model], QuerySet], **kwargs: Any
    ) -> Optional[Any]:
        obj = get_object_or_none(klass=klass, **kwargs)
        if obj:
            self.check_object_permissions(obj)
        return obj

    def _get_permissions(self) -> Iterable[BasePermission]:
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if not self.context:
            return

        for permission_class in self.context.permission_classes:
            permission_instance = permission_class()
            yield permission_instance

    def check_permissions(self) -> None:
        """
        Check if the request should be permitted.
        Raises an appropriate exception if the request is not permitted.
        """
        for permission in self._get_permissions():
            if (
                self.context
                and self.context.request
                and not permission.has_permission(
                    request=self.context.request, controller=self
                )
            ):
                self.permission_denied(permission)

    def check_object_permissions(self, obj: Union[Any, Model]) -> None:
        """
        Check if the request should be permitted for a given object.
        Raises an appropriate exception if the request is not permitted.
        """
        for permission in self._get_permissions():
            if (
                self.context
                and self.context.request
                and not permission.has_object_permission(
                    request=self.context.request, controller=self, obj=obj
                )
            ):
                self.permission_denied(permission)

    def create_response(
        self, message: Any, status_code: int = 200, headers: DictStrAny = {}
    ) -> HttpResponse:
        content = self.api.renderer.render(self.context.request, message, response_status=status_code)
        content_type = "{}; charset={}".format(
            self.api.renderer.media_type, self.api.renderer.charset
        )
        return HttpResponse(content, status=status_code, content_type=content_type, headers=headers)


@overload
def api_controller() -> Type[ControllerBase]:
    ...


@overload
def api_controller(
    prefix: str = '',
    auth: Any = NOT_SET,
    tags: Union[Optional[List[str]], str] = None,
    permissions: Optional["PermissionType"] = None,
    auto_import: bool = True
) -> APIController:
    ...


def api_controller(
    prefix: Union[str, Type] = '',
    auth: Any = NOT_SET,
    tags: Union[Optional[List[str]], str] = None,
    permissions: Optional["PermissionType"] = None,
    auto_import: bool = True
) -> Union[Type[ControllerBase], APIController]:
    if isinstance(prefix, type):
        _api_controller = APIController(prefix='', auth=auth, tags=tags, permissions=permissions, auto_import=auto_import)
        return _api_controller(prefix)

    return APIController(prefix=prefix, auth=auth, tags=tags, permissions=permissions, auto_import=auto_import)
