from . import constants as c
from .exceptions import EventError
from .utils import http_post


class Subscription:
    """
    A subscription of a session to a service and topic.
    """
    def __init__(self, config, session, data):
        self.config = config
        self.session = session
        self.shark = session.shark
        self.name = data.get('subscription') or ''
        if '.' in self.name:
            self.service, self.topic = self.name.split('.', 1)
        else:
            self.service = self.topic = None
        if self.service in config['SERVICES']:
            self.service_config = config['SERVICES'][self.service]
            extra_fields = self.service_config.get('extra_fields', [])
            self.extra_data = {field: data[field] for field in extra_fields
                               if field in data}
        else:
            self.service_config = None
            self.extra_data = {}

        # order key -> numeric order (the default order key is None)
        self.order_state = {}

    def validate(self):
        if not self.service or not self.topic:
            raise EventError(c.ERR_INVALID_SUBSCRIPTION_FORMAT)

        if self.service_config is None:
            raise EventError(c.ERR_INVALID_SERVICE)

    def prepare_service_data(self):
        """
        Returns a data dict to be sent to the service handler.
        """
        data = {'subscription': self.name}
        data.update(self.extra_data)
        data.update(self.session.auth_info)
        return data

    async def perform_service_request(self, service_event, extra_data={},
                                      error_message=None, raise_error=True):
        if service_event in self.service_config:
            url = self.service_config[service_event]
            data = self.prepare_service_data()
            data.update(extra_data)
            result = await http_post(self.shark, url, data)
            if raise_error and result.get('status') != 'ok':
                raise EventError(result.get('error', error_message or
                                            c.ERR_UNHANDLED_EXCEPTION))
            return result
        return {'status': 'ok'}

    async def authorize_subscription(self):
        await self.perform_service_request('authorizer',
                                           error_message=c.ERR_UNAUTHORIZED)

    async def before_subscribe(self):
        return await self.perform_service_request('before_subscribe')

    async def on_subscribe(self):
        return await self.perform_service_request('on_subscribe',
                                                  raise_error=False)

    async def on_message(self, message_data):
        return await self.perform_service_request('on_message', extra_data={
            'data': message_data,
        })

    async def before_unsubscribe(self, raise_error=True):
        return await self.perform_service_request('before_unsubscribe',
                                                  raise_error=raise_error)

    async def on_unsubscribe(self):
        return await self.perform_service_request('on_unsubscribe',
                                                  raise_error=False)

    def _should_deliver_message_filter_fields(self, data):
        """
        Returns whether to deliver the given message based on filter feilds.
        """
        # Check whether the message is filtered by comparing any defined
        # filter_fields to auth_info.
        filter_fields = self.service_config.get('filter_fields', [])
        for field in filter_fields:
            if field in data:
                if self.session.auth_info.get(field) != data[field]:
                    # Message doesn't match auth fields.
                    return False
        return True

    def _should_deliver_message_order(self, data):
        """
        Returns whether to deliver the given message based on order.
        """
        # Check whether the message is out-of-order.
        if '_order' in data:
            key = data.get('_order_key')
            last_order = self.order_state.get(key)

            try:
                order = int(data['_order'])
            except (TypeError, ValueError):
                return False  # Don't deliver messages with invalid orders.

            if last_order is not None and order <= last_order:
                return False  # Message out-of-order.

            self.order_state[key] = order
        return True

    def should_deliver_message(self, data):
        """
        Returns whether to deliver the given message.
        """
        if not self._should_deliver_message_filter_fields(data):
            self.session.log.debug('message filtered', data=data,
                                   reason='fields')
            return False

        if not self._should_deliver_message_order(data):
            self.session.log.debug('message filtered', data=data,
                                   reason='order')
            return False

        return True

    async def subscribe(self, event):
        """
        Subscribes to the subscription.
        """
        require_authentication = self.service_config.get(
            'require_authentication', True)

        if require_authentication and not self.session.auth_info:
            raise EventError(c.ERR_AUTH_REQUIRED)

        if self.name in self.session.subscriptions:
            raise EventError(c.ERR_ALREADY_SUBSCRIBED)

        await self.authorize_subscription()

        await self.shark.service_receiver.add_provisional_subscription(
            self.session, self.name)

        result = await self.before_subscribe()

        self.session.subscriptions[self.name] = self

        if self.should_deliver_message(result):
            await event.send_ok(result.get('data'))

        await self.shark.service_receiver.confirm_subscription(
            self.session, self.name)

        await self.on_subscribe()

    async def message(self, event):
        """
        Sends a message to the subscription.
        """
        if self.name not in self.session.subscriptions:
            raise EventError(c.ERR_SUBSCRIPTION_NOT_FOUND)

        message_data = event.data.get('data')

        result = await self.on_message(message_data)
        if 'data' in result:
            if event:
                await event.send_ok(result['data'])

    async def unsubscribe(self, event):
        """
        Unsubscribes from the subscription.
        """
        if self.name not in self.session.subscriptions:
            raise EventError(c.ERR_SUBSCRIPTION_NOT_FOUND)

        result = await self.before_unsubscribe()

        del self.session.subscriptions[self.name]
        await self.shark.service_receiver.delete_subscription(
            self.session, self.name)

        await event.send_ok(result.get('data'))

        await self.on_unsubscribe()

    async def force_unsubscribe(self):
        """
        Force-unsubscribes from the subscription. Caller is responsible for
        deleting the subscription from the session's subscriptions array.
        This method is called when a session is disconnected.
        """
        await self.shark.service_receiver.delete_subscription(
            self.session, self.name)

        await self.before_unsubscribe(raise_error=False)
        await self.on_unsubscribe()
