from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

from aioprometheus import REGISTRY, Counter, Registry
from aioprometheus.mypy_types import LabelsType

Scope = Dict[str, Any]
Message = Dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGICallable = Callable[[Scope, Receive, Send], Awaitable[None]]


EXCLUDE_PATHS = (
    "/metrics",
    "/metrics/",
    "/docs",
    "/openapi.json",
    "/docs/oauth2-redirect",
    "/redoc",
    "/favicon.ico",
)


class MetricsMiddleware:
    """This class implements a Prometheus metrics collection middleware for
    ASGI applications.

    The default metrics provided by this middleware include counters for
    requests received, responses sent, exceptions raised and status codes
    for route handlers.

    :param app: An ASGI callable. This callable represents the next ASGI
      callable in the chain which might be the application or another
      middleware.

    :param registry: A collector registry to use when rendering metrics. If
      not specified then the default registry will be used.

    :param exclude_paths: A list of urls that should not trigger updates to
      the default metrics.

    :param use_template_urls: A boolean that defines whether route template
      URLs should be used by the default route monitoring metrics. Template
      URLs will report '/users/{user_id}' instead of '/users/bob' or
      '/users/alice', etc. The template URLS can be more useful than the
      actual route url as they allow the route handler to be easily
      identified. This feature is only supported with Starlette / FastAPI
      currently. Default value is True.

    :param group_status_codes: A boolean that defines whether status codes
      should be grouped under a value representing that code kind. For
      example, 200, 201, etc will all be grouped under 2xx. The default value
      is False which means that status codes are not grouped.
    """

    def __init__(
        self,
        app: ASGICallable,
        registry: Registry = REGISTRY,
        exclude_paths: Sequence[str] = EXCLUDE_PATHS,
        use_template_urls: bool = True,
        group_status_codes: bool = False,
        const_labels: Optional[LabelsType] = None,
    ) -> None:
        # The 'app' argument really represents an ASGI framework callable.
        self.asgi_callable = app

        # A reference to the ASGI app is used to assist when extracting
        # route template patterns. Only Starlette/FastAPI apps currently
        # provide this feature. In normal operations the app reference is
        # obtained from the 'lifespan' scope.
        self.starlette_app = None

        self.exclude_paths = exclude_paths if exclude_paths else []
        self.use_template_urls = use_template_urls
        self.group_status_codes = group_status_codes

        if registry is not None and not isinstance(registry, Registry):
            raise Exception(f"registry must be a Registry, got: {type(registry)}")
        self.registry = registry

        self.const_labels = const_labels

        # The creation of the middleware metrics is delayed until the first
        # call to update one of the metrics. This ensures that the metrics
        # are only created once - even in situations such as Starlette's
        # occasional middleware rebuilding that creates new instances of
        # middleware. This avoids exceptions being raised by the registry
        # when identical metrics collectors are created.
        self.metrics_created = False

    def create_metrics(self):
        """Create middleware metrics"""

        self.requests_counter = (  # pylint: disable=attribute-defined-outside-init
            Counter(
                "requests_total_counter",
                "Total number of requests received",
                const_labels=self.const_labels,
                registry=self.registry,
            )
        )

        self.responses_counter = (  # pylint: disable=attribute-defined-outside-init
            Counter(
                "responses_total_counter",
                "Total number of responses sent",
                const_labels=self.const_labels,
                registry=self.registry,
            )
        )

        self.exceptions_counter = (  # pylint: disable=attribute-defined-outside-init
            Counter(
                "exceptions_total_counter",
                "Total number of requested which generated an exception",
                const_labels=self.const_labels,
                registry=self.registry,
            )
        )

        self.status_codes_counter = (  # pylint: disable=attribute-defined-outside-init
            Counter(
                "status_codes_counter",
                "Total number of response status codes",
                const_labels=self.const_labels,
                registry=self.registry,
            )
        )

        self.metrics_created = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if not self.metrics_created:
            self.create_metrics()

        if self.starlette_app is None:
            # To assist with extracting route templates later the middleware
            # needs a reference to the starlette app. Fetch it from the scope.
            # In normal operations this can be found in the 'lifespan' scope.
            # However, in unit tests that use the starlette httpx test client
            # it appears that the ASGI 'lifespan' call is not made. In this
            # scenario obtain the app reference from the 'http' scope.
            if scope["type"] in ("lifespan", "http", "websocket"):
                self.starlette_app = scope.get("app")

        if scope["type"] == "lifespan":
            await self.asgi_callable(scope, receive, send)
            return

        if scope["type"] in ("http", "websocket"):

            def wrapped_send(response):
                """
                Wrap the ASGI send function so that metrics collection can be finished.
                """
                # This function makes use of labels defined in the calling context.

                if response["type"] == "http.response.start":
                    status_code_labels = labels.copy()
                    status_code = str(response["status"])
                    status_code_labels["status_code"] = (
                        f"{status_code[0]}xx"
                        if self.group_status_codes
                        else status_code
                    )
                    self.status_codes_counter.inc(status_code_labels)
                    self.responses_counter.inc(labels)

                return send(response)

            # Store HTTP path and method so they can be used later in the send
            # method to complete metrics updates.
            method = scope.get("method")
            path = self.get_full_or_template_path(scope)
            labels = {"method": method, "path": path}

            if path in self.exclude_paths:
                await self.asgi_callable(scope, receive, send)
                return

            self.requests_counter.inc(labels)
            try:
                await self.asgi_callable(scope, receive, wrapped_send)
            except Exception:
                self.exceptions_counter.inc(labels)

                status_code_labels = labels.copy()
                status_code_labels["status_code"] = (
                    "5xx" if self.group_status_codes else "500"
                )
                self.status_codes_counter.inc(status_code_labels)
                self.responses_counter.inc(labels)

                raise

    def get_full_or_template_path(self, scope) -> str:
        """
        Using the route template url can be more insightful than the actual
        route url so that the route handler function can be easily identified.

        For example, seeing the path '/users/{user_id}' in metrics is often
        better than every combination of '/users/bob', /users/alice', etc.

        Obtaining the route template will be a unique procedure for each web
        framework. This feature is currently only supported for Starlette
        and FastAPI applications.
        """
        root_path = scope.get("root_path", "")
        path = scope.get("path", "")
        full_path = f"{root_path}{path}"

        if self.use_template_urls:
            if self.starlette_app:
                # Extract the route template from Starlette / FastAPI apps
                for route in self.starlette_app.routes:
                    match, _child_scope = route.matches(scope)
                    # Enum value 2 represents the route template Match.FULL
                    if match.value == 2:
                        return route.path

        return full_path
