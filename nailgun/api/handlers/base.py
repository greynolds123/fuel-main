# -*- coding: utf-8 -*-

import json
import uuid
import logging

import web
import netaddr

import rpc
from settings import settings
from provision import ProvisionConfig
from provision import ProvisionFactory
from provision.model.profile import Profile as ProvisionProfile
from provision.model.node import Node as ProvisionNode
from provision.model.power import Power as ProvisionPower
from api.models import Release, Cluster, Node, Network, Vlan
from api.models import Task
from network import manager as netmanager


def check_client_content_type(handler):
    content_type = web.ctx.env.get("CONTENT_TYPE", "application/json")
    if web.ctx.path.startswith("/api")\
            and not content_type.startswith("application/json"):
        raise web.unsupportedmediatype
    return handler()

handlers = {}


class HandlerRegistrator(type):
    def __init__(cls, name, bases, dct):
        super(HandlerRegistrator, cls).__init__(name, bases, dct)
        if hasattr(cls, 'model'):
            key = cls.model.__name__
            if key in handlers:
                logging.warning("Handler for %s already registered" % key)
                return
                #raise Exception("Handler for %s already registered" % key)
            handlers[key] = cls


class JSONHandler(object):
    __metaclass__ = HandlerRegistrator

    fields = []

    @classmethod
    def render(cls, instance, fields=None):
        json_data = {}
        use_fields = fields if fields else cls.fields
        if not use_fields:
            raise ValueError("No fields for serialize")
        for field in use_fields:
            if isinstance(field, (tuple,)):
                if field[1] == '*':
                    subfields = None
                else:
                    subfields = field[1:]

                value = getattr(instance, field[0])
                rel = getattr(
                    instance.__class__, field[0]).impl.__class__.__name__
                if value is None:
                    pass
                elif rel == 'ScalarObjectAttributeImpl':
                    handler = handlers[value.__class__.__name__]
                    json_data[field[0]] = handler.render(
                        value, fields=subfields
                    )
                elif rel == 'CollectionAttributeImpl':
                    if not value:
                        json_data[field[0]] = []
                    else:
                        handler = handlers[value[0].__class__.__name__]
                        json_data[field[0]] = [
                            handler.render(v, fields=subfields) for v in value
                        ]
            else:
                value = getattr(instance, field)
                if value is None:
                    pass
                else:
                    f = getattr(instance.__class__, field)
                    if hasattr(f, "impl"):
                        rel = f.impl.__class__.__name__
                        if rel == 'ScalarObjectAttributeImpl':
                            json_data[field] = value.id
                        elif rel == 'CollectionAttributeImpl':
                            json_data[field] = [v.id for v in value]
                        else:
                            json_data[field] = value
                    else:
                        json_data[field] = value
        return json_data
