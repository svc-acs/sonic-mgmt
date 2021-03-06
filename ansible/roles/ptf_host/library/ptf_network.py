#!/usr/bin/python

import subprocess
from docker import Client
from ansible.module_utils.basic import *

DOCUMENTATION = '''
---
module: ptf_network.py
version_added: "0.2"
author: Pavel Shirshov (pavelsh@microsoft.com)
short_description: Generate virtual network for a ptf container
description:
    - This module generates 32 (by default) fp internal network interfaces with names 'eth0'..'eth31', and internal management interface with a name 'mgmt'.
    - The internal fp network interfaces are linux vlan interfaces encapsulated inside of a container.
    - The management interface is connected to host mgmt bridge using veth pair.

Parameters:
    - ptf_name: name of a ptf container
    - ctr_num: a number of ptf container
    - iface: external interface, parent for vlans
    - num_of_ports: number of FP ports, 32 by default
    - vlan_base: the first vlan for the network
    - fp_mtu: MTU for FP ports
    - mgmt_ip_addr: ip address for mgmt port (with network length)
    - mgmt_ip_gw: default gateway for mgmt address
    - mgmt_bridge: a name of the management bridge (host network)

'''

EXAMPLES = '''
- name: Create internal network for the docker container
  ptf_network:
    id: 3
    ptf_name: ptf_{{ id }}
    ctr_num: "{{ id }}"
    iface: "{{ ptf_external_iface }}"
    vlan_base: 101
    mgmt_ip_addr: "10.255.0.198"
    mgmt_ip_gw: "10.255.0.1"
    mgmt_bridge: "br1"

'''


DEFAULT_MTU = 0
DEFAULT_N_PORTS = 32


class PTFNetwork(object):

    def __init__(self, ptf_name, ctr_num, iface, fp_mtu=DEFAULT_MTU):
        self.ptf_name = ptf_name
        self.ctr_num = ctr_num
        self.iface = iface
        self.fp_mtu = fp_mtu

        self.pid = PTFNetwork.get_pid(ptf_name)

        return

    @staticmethod
    def get_pid(ptf_name):
        cli = Client(base_url='unix://var/run/docker.sock')
        result = cli.inspect_container(ptf_name)

        return result['State']['Pid']

    @staticmethod
    def cmd(cmdline):
        cmd = cmdline.split(' ')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        ret_code = process.returncode

        if ret_code != 0:
            raise Exception("ret_code=%d, error message=%s. cmd=%s" % (ret_code, stderr, cmdline))

        return stdout

    @staticmethod
    def brctl(cmdline):
        out = PTFNetwork.cmd(cmdline)

        br_to_ifs = {}
        if_to_br = {}

        rows = out.split('\n')[1:]
        cur_br = None
        for row in rows:
            if len(row) == 0:
                continue
            terms = row.split()
            if not row[0].isspace():
                cur_br = terms[0]
                br_to_ifs[cur_br] = []
                if len(terms) > 3:
                    br_to_ifs[cur_br].append(terms[3])
                    if_to_br[terms[3]] = cur_br
            else:
                br_to_ifs[cur_br].append(terms[0])
                if_to_br[terms[0]] = cur_br

        return br_to_ifs, if_to_br

    @staticmethod
    def ifconfig(cmdline):
        out = PTFNetwork.cmd(cmdline)

        ifaces = set()

        rows = out.split('\n')
        for row in rows:
            if len(row) == 0:
                continue
            terms = row.split()
            if not row[0].isspace():
                ifaces.add(terms[0])

        return ifaces

    def update(self):
        self.host_br_to_ifs, self.host_if_to_br = PTFNetwork.brctl('brctl show')
        self.host_ifaces = PTFNetwork.ifconfig('ifconfig -a')
        self.ctr_ifaces = PTFNetwork.ifconfig('nsenter -t %s -n ifconfig -a' % self.pid)

        return

    def add_br_if_to_docker(self, bridge, ext_if, int_if):
        self.update()

        if ext_if not in self.host_ifaces:
            cmd1 = "ip link add %s type veth peer name %s" % (ext_if, int_if)
            PTFNetwork.cmd(cmd1)

        if ext_if not in self.host_if_to_br:
            cmd2 = "brctl addif %s %s" % (bridge, ext_if)
            PTFNetwork.cmd(cmd2)

        cmd3 = "ip link set %s up" % ext_if
        PTFNetwork.cmd(cmd3)

        self.update()

        if int_if in self.host_ifaces and int_if not in self.ctr_ifaces:
            cmd4 = "ip link set netns %s dev %s" % (self.pid, int_if)
            PTFNetwork.cmd(cmd4)

        self.update()

        cmd5 = "nsenter -t %s -n ip link set %s up" % (self.pid, int_if)
        PTFNetwork.cmd(cmd5)

        return

    def add_ip_to_int_if(self, int_if, mgmt_ip_addr, mgmt_gw):
        self.update()
        if int_if in self.ctr_ifaces:
            cmd_1 = "nsenter -t %s -n ip addr flush dev %s" % (self.pid, int_if)
            PTFNetwork.cmd(cmd_1)
            cmd_2 = "nsenter -t %s -n ip addr add %s dev %s" % (self.pid, mgmt_ip_addr, int_if)
            PTFNetwork.cmd(cmd_2)
            cmd_3 = "nsenter -t %s -n ip route add default via %s dev %s" % (self.pid, mgmt_gw, int_if )
            PTFNetwork.cmd(cmd_3)

        return

    def change_phy_if_mtu(self, iface_name):
        if self.fp_mtu == DEFAULT_MTU:
            return

        self.update()

        if iface_name in self.ctr_ifaces:
            cmd = "nsenter -t %s -n ip link set dev %s mtu %d" % (self.pid, iface_name, self.fp_mtu)
            PTFNetwork.cmd(cmd)
        else:
            raise Exception("Internal interface: %s doesn't exist" % iface_name)

        return

    def add_phy_if_to_docker(self, iface_name, vlan):
        int_if = "%s.%d" % (self.iface, vlan)

        if int_if not in self.host_ifaces and iface_name not in self.ctr_ifaces and int_if not in self.ctr_ifaces:
            cmd1 = "vconfig add %s %s" % (self.iface, vlan)
            PTFNetwork.cmd(cmd1)

        self.update()

        if int_if in self.host_ifaces and int_if not in self.ctr_ifaces and iface_name not in self.ctr_ifaces:
            cmd2 = "ip link set netns %s dev %s" % (self.pid, int_if)
            PTFNetwork.cmd(cmd2)

        self.update()

        if int_if in self.ctr_ifaces and iface_name not in self.ctr_ifaces:
            cmd3 = "nsenter -t %s -n ip link set dev %s name %s" % (self.pid, int_if, iface_name)
            PTFNetwork.cmd(cmd3)

        cmd4 = "nsenter -t %s -n ip link set %s up" % (self.pid, iface_name)
        PTFNetwork.cmd(cmd4)

        return

    def add_fp_ports(self, num_of_ports, vlan_base):
        if self.iface in self.host_ifaces:
            cmd = "ip link set %s up" % self.iface
            PTFNetwork.cmd(cmd)

            if self.fp_mtu != DEFAULT_MTU:
                cmd = "ip link set dev %s mtu %d" % (self.iface, self.fp_mtu)
                PTFNetwork.cmd(cmd)
        else:
            raise Exception("External interface: %s doesn't exist" % self.iface)

        for i in xrange(num_of_ports):
            vlan = vlan_base + i
            iface_name = "eth%d" % i
            self.add_phy_if_to_docker(iface_name, vlan)
            self.change_phy_if_mtu(iface_name)

        return

    def add_mgmt_port(self, mgmt_bridge, mgmt_ip, mgmt_gw):
        self.add_br_if_to_docker(mgmt_bridge, "ptf-mgmt-%d" % self.ctr_num, "mgmt")
        self.add_ip_to_int_if("mgmt", mgmt_ip, mgmt_gw)

        return


def main():
    module = AnsibleModule(
        argument_spec=dict(
            ptf_name=dict(required=True, type='str'),
            ctr_num=dict(required=True, type='int'),
            iface=dict(required=True, type='str'),
            num_of_ports=dict(required=False, type='int', default=DEFAULT_N_PORTS),
            vlan_base=dict(required=True, type='int'),
            fp_mtu=dict(required=False, type='int', default=DEFAULT_MTU),
            mgmt_ip_addr=dict(required=True, type='str'),
            mgmt_ip_gw=dict(required=True, type='str'),
            mgmt_bridge=dict(required=True, type='str')),
        supports_check_mode=False)

    ptf_name = module.params['ptf_name']
    ctr_num = module.params['ctr_num']
    iface = module.params['iface']
    num_of_ports = module.params['num_of_ports']
    vlan_base = module.params['vlan_base']
    fp_mtu = module.params['fp_mtu']
    mgmt_ip_addr = module.params['mgmt_ip_addr']
    mgmt_ip_gw = module.params['mgmt_ip_gw']
    mgmt_bridge = module.params['mgmt_bridge']

    try:
        net = PTFNetwork(ptf_name, ctr_num, iface, fp_mtu)
        net.add_mgmt_port(mgmt_bridge, mgmt_ip_addr, mgmt_ip_gw)
        net.add_fp_ports(num_of_ports, vlan_base)

    except Exception as error:
        module.fail_json(msg=str(error))

    module.exit_json(changed=True)

if __name__ == "__main__":
    main()

