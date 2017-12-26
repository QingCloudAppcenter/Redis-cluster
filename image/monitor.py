#!/usr/bin/python

import os
import sys
import json
import subprocess

REDIS_STATIC_KEYS = ['used_memory', 
                     'total_connections_received', 
                     'connected_clients', 
                     'expired_keys',
                     'evicted_keys',
                     'keyspace_hits',
                     'keyspace_misses',
                     ]
REDIS_SET_CMDS = ['set', 'mset', 'hmset', 'hset', 'lset', 'getset', 'msetnx', 'psetex', 'setbit', 
                  'setex', 'setnx', 'setrange', 'hsetnx']
REDIS_GET_CMDS = ['get', 'getbit', 'getrange', 'mget', 'hget', 'hgetall', 'hmget']
REDIS_KEY_BASED_CMDS = ['del', 'dump', 'exists', 'expire', 'expireat', 'keys', 'migrate', 'move',
                        'object', 'persist', 'pexpire', 'pexpireat', 'pttl', 'randomkey', 'rename',
                        'renamenx', 'restore', 'sort', 'ttl', 'type', 'scan']
REDIS_STRING_BASED_CMDS = ['append', 'bitcount', 'bitop', 'bitpos', 'decr', 'decrby', 'get', 'getbit',
                           'getrange', 'getset', 'incr', 'incrby', 'incrbyfloat', 'mget', 'mset', 
                           'msetnx', 'psetex', 'set', 'setbit', 'setex', 'setnx', 'setrange', 'strlen']
REDIS_HASH_BASED_CMDS = ['hdel', 'hexists', 'hget', 'hgetall', 'hincrby', 'hincrbyfloat', 'hkeys', 'hlen',
                         'hmget', 'hmset', 'hset', 'hsetnx', 'hvals', 'hscan']
REDIS_LIST_BASED_CMDS = ['blpop', 'brpop', 'brpoplpush', 'lindex', 'linsert', 'llen', 'lpop', 'lpush',
                         'lpushx', 'lrange', 'lrem', 'lset', 'ltrim', 'rpop', 'rpoplpush', 'rpush',
                         'rpushx']
REDIS_SET_BASED_CMDS = ['sadd', 'scard', 'sdiff', 'sdiffstore', 'sinter', 'sinterstore', 'sismember',
                        'smembers', 'smove', 'spop', 'srandmember', 'srem', 'sunion', 'sunionstore',
                        'sscan']
REDIS_SORTED_SET_BASED_CMDS = ['zadd', 'zcard', 'zcount', 'zincrby', 'zinterstore', 'zlexcount', 'zrange',
                               'zrangebylex', 'zrevrangebylex', 'zrangebyscore', 'zrank', 'zrem', 'zremrangebylex',
                               'zremrangebyrank', 'zremrangebyscore', 'zrevrange', 'zrevrangebyscore', 'zrevrank',
                               'zscore', 'zunionstore', 'zscan']
REIDS_DBS = ['db0', 'db1', 'db2', 'db3', 'db4', 'db5', 'db6', 'db7', 'db8', 'db9', 'db10', 'db11', 'db12', 'db13', 'db14', 'db15']


REDIS_CONF = '/opt/redis/redis.conf'

def run_cmd(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, error = p.communicate()
    # logging.info('run cmd: %s' % cmd)
    return out.decode('utf-8')

class Monitor:

    def __init__(self):
        pass

    def _parse_redis_value(self, value):
        if value.find(',') < 0:
            return value
        dvalue = {}
        for v in value.split(','):
            items = v.split('=')
            if len(items) != 2:
                continue
            dvalue[items[0]] = items[1]   
            
        return dvalue

    def get_stats_info(self):

        # read password
        password = None
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
                password = requirepass

        if password:
            cmd = '/opt/redis/bin/redis-cli -h 127.0.0.1 -p 6379 -a %s info all' % password
        else:
            cmd = '/opt/redis/bin/redis-cli -h 127.0.0.1 -p 6379 info all'
        info = run_cmd(cmd)

        stats_info = {}
        try:
            for l in info.splitlines():
                items = l.split(':')
                if len(items) != 2:
                    continue
                (k, v) = items
                stats_info[k] = self._parse_redis_value(v)
        except Exception as e:
            logger.error("parse redis info failed [%s]" % e)
            return None
        
        return stats_info

    def collect_data(self):
        info = self.get_stats_info()
        if not info:
            return {}

        values = {}
        key_count = 0
        get_count = 0
        set_count = 0
        key_based_count = 0
        string_based_count = 0
        set_based_count = 0
        sorted_set_based_count = 0
        list_based_count = 0
        hash_based_count = 0
        max_memory = int(info.get("maxmemory", 0))
        used_memory = int(info.get('used_memory', 0))
        memory_usage = 0

        for k, v in info.items():
            #print k, v
            if k.startswith("cmdstat_"):
                items = k.split('_')
                #print k, v, v['calls']
                if len(items) != 2:
                    continue

                if items[1] in REDIS_GET_CMDS:
                    get_count += int(v["calls"])
                elif items[1] in REDIS_SET_CMDS:
                    set_count += int(v["calls"])
                elif items[1] in REDIS_KEY_BASED_CMDS:
                    key_based_count += int(v["calls"])
                elif items[1] in REDIS_STRING_BASED_CMDS:
                    string_based_count += int(v["calls"])
                elif items[1] in REDIS_SET_BASED_CMDS:
                    set_based_count += int(v["calls"])
                elif items[1] in REDIS_SORTED_SET_BASED_CMDS:
                    sorted_set_based_count += int(v["calls"])
                elif items[1] in REDIS_LIST_BASED_CMDS:
                    list_based_count += int(v["calls"])
                elif items[1] in REDIS_HASH_BASED_CMDS:
                    hash_based_count += int(v["calls"])
            elif k in REDIS_STATIC_KEYS:
                values[k] = v
            elif k in REIDS_DBS:
                key_count += int(v["keys"])

        if max_memory <= 0:
            memory_usage = 0
        else:
            memory_usage = round(((float)(used_memory) / max_memory) * 1000, 1)
            memory_usage = int(memory_usage * 10)

        memory_usage = 1000 if memory_usage > 1000 else memory_usage

        keyspace_hits = int(info.get('keyspace_hits', 0))
        keyspace_misses = int(info.get('keyspace_misses', 0))
        hit_rate = 0

        if keyspace_hits == 0 and keyspace_misses == 0:
            hit_rate = 0
        else:
            hit_rate = round(((float)(keyspace_hits) / (keyspace_hits + keyspace_misses)) * 100, 1)
            hit_rate = int(hit_rate * 10)

        values['memory_usage_min'] = memory_usage
        values['memory_usage_avg'] = memory_usage
        values['memory_usage_max'] = memory_usage
        values['keyspace_hits'] = keyspace_hits
        values['keyspace_misses'] = keyspace_misses
        values['hit_rate_min'] = hit_rate
        values['hit_rate_avg'] = hit_rate
        values['hit_rate_max'] = hit_rate
        values['set_count'] = set_count
        values['get_count'] = get_count
        values['key_based_count'] = key_based_count
        values['string_based_count'] = string_based_count
        values['set_based_count'] = set_based_count
        values['sorted_set_based_count'] = sorted_set_based_count
        values['list_based_count'] = list_based_count
        values['hash_based_count'] = hash_based_count
        values['key_count'] = key_count

        if 'connected_clients' in values:
            values['connected_clients_min'] = values['connected_clients']
            values['connected_clients_avg'] = values['connected_clients']
            values['connected_clients_max'] = values['connected_clients']


        return values

if __name__=="__main__":

    monitor = Monitor()#, require_pass=True, password="Pa88w0rd")
    print(json.dumps(monitor.collect_data()))
