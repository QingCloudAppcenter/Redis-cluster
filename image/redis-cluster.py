import logging
import subprocess
import time
import sys
import json

REDIS_CLI_PATH = '/opt/redis/bin/redis-cli'
REDIS_TRIB_PATH = '/opt/redis/bin/redis-trib.rb'
SCALING_OUT_INFO = '/opt/redis/scaling-out.info'
SCALING_IN_INFO = '/opt/redis/scaling-in.info'
CLUSTER_NODES_INFO = '/opt/redis/nodes.info'
REDIS_PORT = '6379'
LOG_FILE = '/data/redis/logs/redis.log'
RESHARD_LOG = '/data/redis/logs/redis_reshard.log'
REDIS_CONF = '/opt/redis/redis.conf'

class RedisNode:
    def __init__(self, role, ip, gid):
        self.role = role
        self.ip = ip
        self.gid = gid
        self.id = None



class RedisCluster:
    def __init__(self):
        self.port = REDIS_PORT
        self.cluster_endpoint = None#get_cluster_endpoint()
        self.nodes = []
        self.masters = []
        self.slaves = []
        self.active_nodes_ip = []
        self.init_nodes()

        # read password
        self.password = None
        with open(REDIS_CONF) as f:
            lines = f.readlines()
            masterauth = ""
            requirepass = ""
            for line in lines:
                conf = line.split()
                if len(conf) >= 2:
                    key = conf[0]
                    value = conf[1]
                    if key == 'masterauth':
                        masterauth = value
                    if key == 'requirepass':
                        requirepass = value

            if requirepass != "" and masterauth == requirepass:
                logging.info("Setting password from redis.conf %s" % requirepass)
                self.password = requirepass
            



    # When adding nodes, nodes info will be written into metadata service first, 
    # they haven't been joined in cluster at that time.
    def init_nodes(self):
        with open(CLUSTER_NODES_INFO) as f:
            lines = f.readlines()
            for line in lines:
                # format: M:192.168.0.6:$gid
                line = line.rstrip().split(':')
                if len(line) != 3 :
                    continue
                node = RedisNode(line[0], line[1], line[2])
                self.nodes.append(node)
                if line[0] == 'M':
                    self.masters.append(node)
                elif line[0] == 'S':
                    self.slaves.append(node)

        self.cluster_endpoint = '%s:%s' % (self.masters[0].ip, self.port)
        # logging.info('Current cluster nodes: %s' % self.nodes)

    def create_cluster(self):

        # init with masters only
        all_masters = ''
        for master in self.masters:
            all_masters = '%s %s:%s' % (all_masters, master.ip, REDIS_PORT)
        if self.password:
            cmd = 'echo yes | /opt/redis/bin/redis-trib.rb create --password %s %s > /var/log/init_redis.log 2>&1' % (self.password, all_masters)
        else:
            cmd = 'echo yes | /opt/redis/bin/redis-trib.rb create %s > /var/log/init_redis.log 2>&1' % all_masters
        run_cmd(cmd)

        # check cluster status
        self.active_nodes_ip = [master.ip for master in self.masters]
        self.check_cluster_status_ok()

        self.update_cluster_nodes_info()

        # add slaves
        for slave in self.slaves:
            self.add_slave(slave.ip)
            self.active_nodes_ip.append(slave.ip)

    def add_master(self, new_master_ips):
        # check cluster status
        self.check_cluster_status_ok()

        # add master nodes to cluster
        logging.info('[Step 0] Executing add-node cmd via redis-trib.rb')
        for master in new_master_ips:
            if self.password:
                cmd = '%s add-node --password %s %s:%s %s' % (REDIS_TRIB_PATH, self.password,
                    master, REDIS_PORT, self.cluster_endpoint)
            else:
                cmd = '%s add-node %s:%s %s' % (REDIS_TRIB_PATH, 
                    master, REDIS_PORT, self.cluster_endpoint)
            time.sleep(1)
            
            retry_times = 10
            while True:
                ret = run_cmd(cmd, verbose = True)
                succeed_flag = '[OK] New node added correctly.'
                if succeed_flag in ret:
                    logging.info('[Step 0] add-node cmd executed successfully: %s' % master)
                    break
                else:
                    logging.error('Cannot add node to cluster: output is %s' % ret)
                    if retry_times != 0:
                        retry_times = retry_times - 1
                        logging.info('%s times to retry' % retry_times)
                        time.sleep(1)
                        continue
                    else:
                        return -1

        # wait cluster status update
        new_nodes_status = self.check_nodes_status_ok(new_master_ips)
        if new_nodes_status != 'ok':
            logging.error('Please check status on %s' % new_master_ips)
            sys.exit(-1)
        self.active_nodes_ip = self.active_nodes_ip + new_master_ips

        # get cluster info
        logging.info('[Step 1] Getting cluster info')
        cluster_info = self.get_redis_cluster_info()
        allocated_master_server_ids = []
        unallocated_master_server_ids = []
        unallocated_master_private_ips = []
        for server_id, info in cluster_info.items():
            role = info["role"]
            if role == 'master':
                num = info["num"]
                if num == 0 or num == "0":
                    unallocated_master_server_ids.append(server_id)
                    ip = info["socket"][:-(len(REDIS_PORT)+1)]
                    unallocated_master_private_ips.append(ip)
                else:
                    allocated_master_server_ids.append(server_id)        
        source = ",".join(allocated_master_server_ids)
        slots_per_master = 16384 / (len(allocated_master_server_ids)+len(unallocated_master_server_ids))
        logging.info('[Step 1] Got cluster info successfully, source is %s, slots_per_master is %s' % (source, slots_per_master))

        # check if new masters are ready
        logging.info('[Step 2] Check if new masters are ready: %s' % unallocated_master_private_ips)
        new_masters_status = self.check_nodes_status_ok(unallocated_master_private_ips)
        logging.info('[Step 2] Status of new masters is %s' % new_masters_status)
        if new_masters_status != 'ok':
            logging.error('Please check new masters status')
            sys.exit(-1)

        # reshard
        logging.info('[Step 3] Starting reshard for %s' % unallocated_master_server_ids)
        for target in unallocated_master_server_ids:
            if self.password:          
                cmd = "%s reshard --password %s --from %s --to %s --slots %s --yes %s >> %s" % \
                    (REDIS_TRIB_PATH, self.password, source, target, slots_per_master, self.cluster_endpoint, RESHARD_LOG) 
            else:
                cmd = "%s reshard --from %s --to %s --slots %s --yes %s >> %s" % \
                (REDIS_TRIB_PATH, source, target, slots_per_master, self.cluster_endpoint, RESHARD_LOG) 
            logging.info("Resharding the redis cluster [%s]...", cmd)
            ret = run_cmd(cmd)
            reshard_status = self.check_cluster_slots_ok()
            if reshard_status != 'ok':
                logging.error('Please check reshard status')
                sys.exit(-1)
            logging.info("[Step 3] Reshard the redis cluster [%s] successful", cmd)

    def add_slave(self, slave):
        # check cluster status
        self.check_cluster_status_ok()

        master_id = self.get_master_id_by_slave_ip(slave)

        logging.info('[Step 0] Add node %s as slave' % slave)
        if self.password:
            cmd = '%s add-node --password %s --slave --master-id %s %s:%s %s' % (REDIS_TRIB_PATH, self.password, master_id, slave, REDIS_PORT, self.cluster_endpoint)
        else:
            cmd = '%s add-node --slave --master-id %s %s:%s %s' % (REDIS_TRIB_PATH, master_id, slave, REDIS_PORT, self.cluster_endpoint)
        ret = run_cmd(cmd)
        time.sleep(1)

        logging.info('[Step 1] Check node status')
        new_replica_status = self.check_nodes_status_ok([slave])
        logging.info('[Step 1] Status of new replica is %s' % new_replica_status)
        if new_replica_status != 'ok':
            logging.error('Please check node %s status' % slave)
            sys.exit(-1)


    # nodes_info = {'master': [ip1, ip2...], 'master-replica':[ip1, ip2...]}
    def check_nodes_role_matched(self, nodes_info): 
        # check master
        for ip in nodes_info['master']:
            logging.info('checking whether master node %s role changed' % ip)
            for node in self.masters:
                if node.ip == ip and node.role != 'M':
                    logging.error('Node has been failovered, the role changed to: %s' % node.role)
                    sys.exit(-1)

        # check master-replica
        for ip in nodes_info['master-replica']:
            logging.info('checking whether slave node %s role changed' % ip)
            for node in self.slaves:
                if node.ip == ip and node.role != 'S':
                    logging.error('Node has been failovered, the role changed to: %s' % node.role)
                    sys.exit(-1)


    def check_cluster_status_ok(self):
        logging.info('Checking if cluster are ready')
    
        # check cluster status
        cluster_status = self.check_nodes_status_ok(self.active_nodes_ip)

        # double check
        slots_status = self.check_cluster_slots_ok()

        logging.info('Status of cluster is %s, slots status is %s' % (cluster_status, slots_status))
        if cluster_status != 'ok' or slots_status != 'ok':
            logging.error('Please check cluster status')
            sys.exit(-1)


    def check_nodes_status_ok(self, nodes):
        max_tries = 20
        status = 'unknown'
        for ip in nodes:
            tries = 0
            status = 'unknown'
            if self.password:
                cmd = "%s -h %s -p %s -a %s cluster info | grep cluster_state" % (REDIS_CLI_PATH, ip, REDIS_PORT, self.password)
            else:
                cmd = "%s -h %s -p %s cluster info | grep cluster_state" % (REDIS_CLI_PATH, ip, REDIS_PORT) 
            while tries < max_tries:
                status_info = run_cmd(cmd).rstrip().split(':')
                if status_info is not None and len(status_info) > 1:
                    status = status_info[1]

                logging.info("Check redis cluste status [time:%s] from instance [%s:%s]: %s", \
                            tries, ip, REDIS_PORT, status)
                if status == "ok":
                    logging.info('Status of node %s is %s' % (ip, status))
                    break

                # fix_cmd = '%s fix %s'
                time.sleep(2)
                tries = tries + 1
            if status != 'ok':
                logging.info('node %s failed' % ip)
        return status


    def check_cluster_slots_ok(self):
        max_tries = 20
        tries = 0

        status = 'unknown'
        succeed_flag = 'OK] All nodes agree about slots configuration.'
        if self.password:
            cmd = '%s check --password %s %s | grep \'%s\'' % (REDIS_TRIB_PATH, self.password, self.cluster_endpoint, succeed_flag)
        else:
            cmd = '%s check %s | grep \'%s\'' % (REDIS_TRIB_PATH, self.cluster_endpoint, succeed_flag)

        while status != 'ok' and tries < max_tries:
            ret = run_cmd(cmd).rstrip()
            status = 'ok' if len(ret) > 0 else 'ongoing'
            time.sleep(2)
            logging.info('Waiting slots conf te be agreed, status: %s' % status)
            tries = tries + 1
        return status

    def update_cluster_nodes_info(self):
        cluster_info = self.get_redis_cluster_info()
        for server_id, info in cluster_info.items():
            ip = info["socket"][:-(len(REDIS_PORT)+1)]
            for node in self.nodes:
                if node.ip == ip:
                    node.id = server_id
                    break
        self.cluster_endpoint = '%s:%s' % (self.masters[0].ip, self.port)

    def get_master_id_by_slave_ip(self, slave_ip):
        gid = None
        for slave in self.slaves:
            if slave.ip == slave_ip:
                gid = slave.gid
                break

        for master in self.masters:
            if master.gid == gid:
                return master.id

    def get_redis_cluster_info(self):
        '''
        Connecting to node 192.168.100.12:6379: OK
        Connecting to node 192.168.100.7:6379: OK
        Connecting to node 192.168.100.9:6379: OK
        Connecting to node 192.168.100.11:6379: OK
        Connecting to node 192.168.100.10:6379: OK
        Connecting to node 192.168.100.8:6379: OK
        >>> Performing Cluster Check (using node 192.168.100.12:6379)
        M: ef07a34ed37a1eb44d43c7a526a91ebc0df9bc0d 192.168.100.12:6379
           slots:10930-16383 (5454 slots) master
           1 additional replica(s)
        S: 3a9f443600daedc2828b7854653616e5dc4dc625 192.168.100.7:6379
           slots: (0 slots) slave
           replicates ef07a34ed37a1eb44d43c7a526a91ebc0df9bc0d
        S: a7796c2a45c736624a7666ce21cab9ff963c5a1e 192.168.100.9:6379
           slots: (0 slots) slave
           replicates e9826f99fe1b786c5ed206a9795f80856b7e5d85
        M: 677044d76f7730c9ecaec34351b941acbbd293f7 192.168.100.11:6379
           slots:2738-8191 (5454 slots) master
           1 additional replica(s)
        S: 1975f3e583ff9d6e450972767161d76dac4eacca 192.168.100.10:6379
           slots: (0 slots) slave
           replicates 677044d76f7730c9ecaec34351b941acbbd293f7
        M: e9826f99fe1b786c5ed206a9795f80856b7e5d85 192.168.100.8:6379
           slots:0-2737,8192-10929 (5476 slots) master
           1 additional replica(s)
        [OK] All nodes agree about slots configuration.
        >>> Check for open slots...
        >>> Check slots coverage...
        [OK] All 16384 slots covered.
        
        Get meaningful info from above and Convert it to the following:
        
        {
        'a7796c2a45c736624a7666ce21cab9ff963c5a1e': 
            {'socket': '192.168.100.9:6379', 'replicates': 'e9826f99fe1b786c5ed206a9795f80856b7e5d85', 'num': '0', 'role': 'S', 'slots': '', 'id': 'a7796c2a45c736624a7666ce21cab9ff963c5a1e'}, 
        '677044d76f7730c9ecaec34351b941acbbd293f7': 
            {'slots': '2738-8191', 'num': '5454', 'role': 'M', 'id': '677044d76f7730c9ecaec34351b941acbbd293f7', 'socket': '192.168.100.11:6379'}, 
        '1975f3e583ff9d6e450972767161d76dac4eacca': 
            {'socket': '192.168.100.10:6379', 'replicates': '677044d76f7730c9ecaec34351b941acbbd293f7', 'num': '0', 'role': 'S', 'slots': '', 'id': '1975f3e583ff9d6e450972767161d76dac4eacca'}, 
        'ef07a34ed37a1eb44d43c7a526a91ebc0df9bc0d': 
            {'slots': '10930-16383', 'num': '5454', 'role': 'M', 'id': 'ef07a34ed37a1eb44d43c7a526a91ebc0df9bc0d', 'socket': '192.168.100.12:6379'}, 
        'e9826f99fe1b786c5ed206a9795f80856b7e5d85': 
            {'slots': '0-2737,8192-10929', 'num': '5476', 'role': 'M', 'id': 'e9826f99fe1b786c5ed206a9795f80856b7e5d85', 'socket': '192.168.100.8:6379'}, 
        '3a9f443600daedc2828b7854653616e5dc4dc625': 
            {'socket': '192.168.100.7:6379', 'replicates': 'ef07a34ed37a1eb44d43c7a526a91ebc0df9bc0d', 'num': '0', 'role': 'S', 'slots': '', 'id': '3a9f443600daedc2828b7854653616e5dc4dc625'}
        }
        '''

        if self.password:
            cmd = '%s check --password %s %s' % (REDIS_TRIB_PATH, self.password, self.cluster_endpoint)
        else:
            cmd = '%s check %s' % (REDIS_TRIB_PATH, self.cluster_endpoint)
        out = run_cmd(cmd)
        
        lines = out.splitlines()
        nodes = {}
        i = 0
        for line in lines:
            line = line.strip()
            if line.startswith("M:"):
                b_line = line.split()
                slots_line = lines[i+1].strip()
                s_line = slots_line.split()
                nodes[b_line[1]] = {"role":'master', "id":b_line[1], "socket":b_line[2], "slots":s_line[0][6:], "num":s_line[1][1:]}
            elif line.startswith("S:") or line.startswith("FS"):
                b_line = line.split()
                slots_line = lines[i+1].strip()
                s_line = slots_line.split()
                replicates_line = lines[i+2].strip()
                r_line = replicates_line.split()
                nodes[b_line[1]] = {"role":'master-replica', "id":b_line[1], "socket":b_line[2], "slots":s_line[0][6:], "num":s_line[1][1:], "replicates":r_line[1]}
            i = i + 1

        logging.info("cluster nodes info: %s", nodes)
        return nodes

    def is_adding_node(self):
        scale_out_info_changed = False
        with open(SCALING_OUT_INFO, 'r') as f:
            lines = f.readlines()
            for line in lines:
                line = line.rstrip().split(' ')
                if len(line) != 2:
                    continue
                scale_out_info_changed = True

                if scale_out_info_changed:
                    return True
        return False


    # scale in
    def reshard_and_scale_in(self, scale_in_info):
        # check cluster status
        self.check_cluster_status_ok()

        # get cluster info
        cluster_info = self.get_redis_cluster_info()

        # TODO remove me after dynamic role feature online
        self.check_nodes_role_matched(scale_in_info)

        # Parse nodes and cluster_info
        existing_master_ids = [] # not deleted masters
        delete_master_info = {}
        delete_replicates_ids = []

        for node_id, node_info in cluster_info.items():
            logging.info('node_id is %s, info is %s' % (node_id, node_info))
            role = node_info['role']
            ip = node_info["socket"][:-(len(REDIS_PORT)+1)]
            if role == 'master':
                if ip in scale_in_info['master']:
                    delete_master_info[node_id] = node_info
                else:
                    existing_master_ids.append(node_id)
            elif role == 'master-replica' and ip in scale_in_info['master-replica']:
                delete_replicates_ids.append(node_id)

        # reshard
        for node_id, node_info in delete_master_info.items():
            slots_num = int(node_info["num"])
            slots_per_master = slots_num / len(existing_master_ids)
            mod_slots = slots_num % len(existing_master_ids)
            
            for target in existing_master_ids:
                num = slots_per_master+mod_slots
                if num <= 0:
                    break
                if self.password:
                    cmd = "%s reshard --password %s --from %s --to %s --slots %s --yes %s >> %s" % \
                    (REDIS_TRIB_PATH, self.password, node_id, target, num, self.cluster_endpoint, RESHARD_LOG)
                else:
                    cmd = "%s reshard --from %s --to %s --slots %s --yes %s >> %s" % \
                    (REDIS_TRIB_PATH, node_id, target, num, self.cluster_endpoint, RESHARD_LOG) 
                logging.info("Resharding the redis cluster, [%s]...", cmd)
                run_cmd(cmd)
                
                reshard_status = self.check_cluster_slots_ok()
                if reshard_status != 'ok':
                    logging.error('Please check reshard status')
                    sys.exit(-1)

                mod_slots = 0 # This variable is only used once
                logging.info("Reshard the redis cluster, [%s] successful", cmd)

        # delete replica
        for node_id in delete_replicates_ids:
            self.check_cluster_status_ok()
            logging.info('Deleting replica %s' % node_id)
            if self.password:
                cmd = '%s del-node --password %s %s %s' % (REDIS_TRIB_PATH, self.password, self.cluster_endpoint, node_id)
            else:
                cmd = '%s del-node %s %s' % (REDIS_TRIB_PATH, self.cluster_endpoint, node_id)
            run_cmd(cmd)

        # delete master
        for node_id, node_info in delete_master_info.items():
            self.check_cluster_status_ok()
            logging.info('Deleting master %s' % node_id)
            if self.password:
                cmd = '%s del-node --password %s %s %s' % (REDIS_TRIB_PATH, self.password, self.cluster_endpoint, node_id)
            else:
                cmd = '%s del-node %s %s' % (REDIS_TRIB_PATH, self.cluster_endpoint, node_id)
            run_cmd(cmd)
            logging.info('Delete master %s successfully, info: %s' % (node_id, node_info))


# When adding nodes, nodes info will be written into metadata service first, 
# they haven't been joined in cluster at that time.
def get_resource_nodes_info():
    nodes_info = {'master': [], 'master-replica': []}
    with open(CLUSTER_NODES_INFO) as f:
        lines = f.readlines()
        for line in lines:
            # format: M:192.168.0.6:$gid
            line = line.rstrip().split(':')
            if len(line) != 3 and line[0] != 'M':
                continue
            if line[0] == 'M':
                nodes_info['master'].append(line[1])
            elif line[0] == 'S':
                nodes_info['master-replica'].append(line[1])

    logging.info('resource nodes info are %s' % nodes_info)
    return nodes_info
                

def get_scale_out_info():
    scale_out_info = {'master': [], 'master-replica': []}
    while True:
        with open(SCALING_OUT_INFO, 'r') as f:
            lines = f.readlines()
            scale_out_info_changed = False
            for line in lines:
                line = line.rstrip().split(' ')
                if len(line) != 2:
                    continue
                role = line[0]
                ip = line[1]
                scale_out_info[role].append(ip)
                scale_out_info_changed = True

            if scale_out_info_changed:
                logging.info('scale_out info: %s' % scale_out_info)
                return scale_out_info

        time.sleep(2)

def get_scale_in_info():
    scale_in_info = {'master': [], 'master-replica': []}
    while True:
        with open(SCALING_IN_INFO, 'r') as f:
            lines = f.readlines()
            scale_in_info_changed = False
            for line in lines:
                line = line.rstrip().split(' ')
                if len(line) != 2:
                    continue
                role = line[0]
                ip = line[1]
                scale_in_info[role].append(ip)
                scale_in_info_changed = True

            if scale_in_info_changed:
                logging.info('scale_in info: %s' % scale_in_info)
                return scale_in_info

        time.sleep(2)


def run_cmd(cmd, verbose=False):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, error = p.communicate()
    logging.info('run cmd: %s' % cmd)
    if verbose:
        logging.info('out is %s' % out)
        logging.info('error is %s' % error)
    return out.decode('utf-8')


if __name__ == '__main__':
    FORMAT = '%(asctime)-15s %(message)s'
    logging.basicConfig(format=FORMAT, filename=LOG_FILE, level=logging.INFO)


    redis_cluster = RedisCluster()

    action = sys.argv[1]

    if action == 'init':
        if not redis_cluster.is_adding_node():
            redis_cluster.create_cluster()
            print('Initiate redis cluster successful')

    elif action == 'health-check':

        # logging.info('Checking redis health status')
        cmd = "%s cluster info | grep cluster_state" % REDIS_CLI_PATH

        if redis_cluster.password:
            cmd = "%s -a %s cluster info | grep cluster_state" % (REDIS_CLI_PATH, redis_cluster.password)
        else:
            cmd = "%s cluster info | grep cluster_state" % REDIS_CLI_PATH

        status_info = run_cmd(cmd).rstrip().split(':')
        if status_info is not None and len(status_info) > 1:
            if status_info[1] != "ok":
                sys.exit(-1)
        else:
            sys.exit(-1)

    elif action == 'roles-check':
        redis_cluster.update_cluster_nodes_info()
        roles = {'labels': ['ip', 'role'], 'data': []}
        for node in redis_cluster.nodes:
            roles['data'].append([node.ip, 'Master' if node.role is 'M' else 'Slave'])
        print(json.dumps(roles))

    elif action == 'scale-out':
        # get cluster endpoint
        all_nodes_info = get_resource_nodes_info()
        scale_out_info = get_scale_out_info()

        current_master_nodes = list(set(all_nodes_info['master']) - set(scale_out_info['master']))
        current_slave_nodes = list(set(all_nodes_info['master-replica']) - set(scale_out_info['master-replica']))

        if len(current_master_nodes) < 0:
            logging.error('No master nodes avaliable')
            sys.exit(-1)

        redis_cluster.active_nodes_ip = current_master_nodes + current_slave_nodes
        redis_cluster.cluster_endpoint = '%s:%s' % (current_master_nodes[0], REDIS_PORT)
        logging.info('cluster endpoint is %s' % redis_cluster.cluster_endpoint)

        # add masters
        if scale_out_info['master']:
            redis_cluster.add_master(scale_out_info['master'])

        # update
        redis_cluster.update_cluster_nodes_info()

        # add slaves
        for replica in scale_out_info['master-replica']:
            redis_cluster.add_slave(replica)
            redis_cluster.active_nodes_ip.append(replica)

        print('Scaling out is successful')

    elif action == 'scale-in':
        # get cluster endpoint
        all_nodes_info = get_resource_nodes_info()
        scale_in_info = get_scale_in_info()

        current_master_nodes = list(set(all_nodes_info['master']) - set(scale_in_info['master']))
        current_slave_nodes = list(set(all_nodes_info['master-replica']) - set(scale_in_info['master-replica']))

        if len(current_master_nodes) < 0:
            logging.error('No master nodes avaliable')
            sys.exit(-1)

        redis_cluster.active_nodes_ip = current_master_nodes + current_slave_nodes
        redis_cluster.cluster_endpoint = '%s:%s' % (current_master_nodes[0], REDIS_PORT)
        logging.info('cluster endpoint is %s' % redis_cluster.cluster_endpoint)

        redis_cluster.check_nodes_status_ok(all_nodes_info['master'] + all_nodes_info['master-replica'])
        # reshard
        redis_cluster.reshard_and_scale_in(scale_in_info)

        print('Scaling in is successful')

    sys.exit(0)