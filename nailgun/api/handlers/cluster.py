# -*- coding: utf-8 -*-

import json
import uuid
import logging
import itertools

import web
import netaddr

import rpc
from settings import settings
from api.models import Cluster, Node, Network, Release, Vlan, Task
from api.handlers.base import JSONHandler
from api.handlers.node import NodeHandler
from provision import ProvisionConfig
from provision import ProvisionFactory
from provision.model.profile import Profile as ProvisionProfile
from provision.model.node import Node as ProvisionNode
from provision.model.power import Power as ProvisionPower
from network import manager as netmanager


class ClusterHandler(JSONHandler):
    fields = (
        "id",
        "name",
        "type",
        "mode",
        "redundancy",
        ("nodes", "*"),
        ("release", "*")
    )
    model = Cluster

    @classmethod
    def render(cls, instance, fields=None):
        json_data = JSONHandler.render(instance, fields=cls.fields)
        json_data["nodes"] = map(
            NodeHandler.render,
            instance.nodes
        )
        return json_data

    def GET(self, cluster_id):
        web.header('Content-Type', 'application/json')
        q = web.ctx.orm.query(Cluster)
        cluster = q.filter(Cluster.id == cluster_id).first()
        if not cluster:
            return web.notfound()
        return json.dumps(
            self.render(cluster),
            indent=4
        )

    def PUT(self, cluster_id):
        web.header('Content-Type', 'application/json')
        q = web.ctx.orm.query(Cluster).filter(Cluster.id == cluster_id)
        cluster = q.first()
        if not cluster:
            return web.notfound()
        # additional validation needed?
        data = Cluster.validate_json(web.data())
        # /additional validation needed?
        for key, value in data.iteritems():
            if key == "nodes":
                map(cluster.nodes.remove, cluster.nodes)
                nodes = web.ctx.orm.query(Node).filter(
                    Node.id.in_(value)
                )
                map(cluster.nodes.append, nodes)
            else:
                setattr(cluster, key, value)
        web.ctx.orm.add(cluster)
        web.ctx.orm.commit()
        return json.dumps(
            self.render(cluster),
            indent=4
        )

    def DELETE(self, cluster_id):
        cluster = web.ctx.orm.query(Cluster).filter(
            Cluster.id == cluster_id
        ).first()
        if not cluster:
            return web.notfound()
        web.ctx.orm.delete(cluster)
        web.ctx.orm.commit()
        raise web.webapi.HTTPError(
            status="204 No Content",
            data=""
        )


class ClusterCollectionHandler(JSONHandler):
    def GET(self):
        web.header('Content-Type', 'application/json')
        return json.dumps(map(
            ClusterHandler.render,
            web.ctx.orm.query(Cluster).all()
        ), indent=4)

    def POST(self):
        web.header('Content-Type', 'application/json')
        data = Cluster.validate(web.data())

        cluster = Cluster()
        cluster.release = web.ctx.orm.query(Release).get(data["release"])

        # TODO: discover how to add multiple objects
        if 'nodes' in data and data['nodes']:
            nodes = web.ctx.orm.query(Node).filter(
                Node.id.in_(data['nodes'])
            )
            map(cluster.nodes.append, nodes)

        # TODO: use fields
        for field in ('name', 'type', 'mode', 'redundancy'):
            setattr(cluster, field, data.get(field))

        web.ctx.orm.add(cluster)
        web.ctx.orm.commit()

        used_nets = [n.cidr for n in web.ctx.orm.query(Network).all()]
        used_vlans = [v.id for v in web.ctx.orm.query(Vlan).all()]

        for network in cluster.release.networks_metadata:
            new_vlan = sorted(list(set(settings.VLANS) - set(used_vlans)))[0]
            vlan_db = Vlan(id=new_vlan)
            web.ctx.orm.add(vlan_db)
            web.ctx.orm.commit()

            pool = settings.NETWORK_POOLS[network['access']]
            nets_free_set = netaddr.IPSet(pool) -\
                netaddr.IPSet(settings.NET_EXCLUDE) -\
                netaddr.IPSet(used_nets)

            free_cidrs = sorted(list(nets_free_set._cidrs))
            new_net = list(free_cidrs[0].subnet(24, count=1))[0]

            nw_db = Network(
                release=cluster.release.id,
                name=network['name'],
                access=network['access'],
                cidr=str(new_net),
                gateway=str(new_net[1]),
                cluster_id=cluster.id,
                vlan_id=vlan_db.id
            )
            web.ctx.orm.add(nw_db)
            web.ctx.orm.commit()

            used_vlans.append(new_vlan)
            used_nets.append(str(new_net))

        raise web.webapi.created(json.dumps(
            ClusterHandler.render(cluster),
            indent=4
        ))


class ClusterChangesHandler(JSONHandler):
    fields = (
        "id",
        "name",
    )

    def PUT(self, cluster_id):
        web.header('Content-Type', 'application/json')
        q = web.ctx.orm.query(Cluster).filter(Cluster.id == cluster_id)
        cluster = q.first()
        if not cluster:
            return web.notfound()

        task = Task(
            uuid=str(uuid.uuid4()),
            name="Provisioning cluster %d" % int(cluster_id)
        )
        web.ctx.orm.add(task)
        web.ctx.orm.commit()

        pc = ProvisionConfig()
        pc.cn = "provision.driver.cobbler.Cobbler"
        pc.url = settings.COBBLER_URL
        pc.user = settings.COBBLER_USER
        pc.password = settings.COBBLER_PASSWORD
        try:
            pd = ProvisionFactory.getInstance(pc)
        except Exception as err:
            raise web.badrequest(str(err))
        pf = ProvisionProfile(settings.COBBLER_PROFILE)
        ndp = ProvisionPower("ssh")
        ndp.power_user = "root"

        allowed_statuses = ("discover", "ready")
        for node in cluster.nodes:
            if node.status not in allowed_statuses:
                raise web.badrequest(
                    "Node %s (%s) status:%s not in %s" % (
                        node.mac,
                        node.ip,
                        node.status,
                        str(allowed_statuses)
                    )
                )

        for node in itertools.ifilter(
            lambda n: n.status in allowed_statuses, cluster.nodes
        ):
            if node.status == "discover":
                logging.info(
                    "Node %s seems booted with bootstrap image",
                    node.id
                )
                ndp.power_pass = "rsa:%s" % settings.PATH_TO_BOOTSTRAP_SSH_KEY
            else:
                logging.info(
                    "Node %s seems booted with real system",
                    node.id
                )
                ndp.power_pass = "rsa:%s" % settings.PATH_TO_SSH_KEY
            ndp.power_address = node.ip

            node.status = "provisioning"
            web.ctx.orm.add(node)
            web.ctx.orm.commit()
            nd = ProvisionNode("%d_%s" % (node.id, node.mac))
            nd.driver = pd
            nd.mac = node.mac
            nd.profile = pf
            nd.pxe = True
            nd.kopts = ""
            nd.power = ndp
            logging.debug(
                "Trying to save node %s into provision system: profile: %s ",
                node.id,
                pf.name
            )
            nd.save()
            logging.debug(
                "Trying to reboot node %s using %s "
                "in order to launch provisioning",
                node.id,
                ndp.power_type
            )
            nd.power_reboot()

        netmanager.assign_ips(cluster_id, "management")

        nodes = []
        for n in cluster.nodes:
            nodes.append({'id': n.id, 'status': n.status,
                          'ip': n.ip, 'mac': n.mac, 'role': n.role,
                          'network_data': netmanager.get_node_networks(n.id)})
        message = {'method': 'deploy',
                   'respond_to': 'deploy_resp',
                   'args': {'task_uuid': task.uuid, 'nodes': nodes}}
        rpc.cast('naily', message)

        return json.dumps(
            self.render(cluster),
            indent=4
        )


class ClusterNetworksHandler(JSONHandler):
    fields = (
        "id",
        "name",
    )

    def PUT(self, cluster_id):
        web.header('Content-Type', 'application/json')
        q = web.ctx.orm.query(Cluster).filter(Cluster.id == cluster_id)
        cluster = q.first()
        if not cluster:
            return web.notfound()

        task = Task(
            uuid=str(uuid.uuid4()),
            name="Verify Networks for cluster %d" % cluster_id
        )
        web.ctx.orm.add(task)
        web.ctx.orm.commit()

        message = {'method': 'verify_networks',
                   'respond_to': 'verify_networks_resp',
                   'args': {'task_uuid': task.uuid}}
        rpc.cast('naily', message)

        return json.dumps(
            self.render(cluster),
            indent=4
        )
