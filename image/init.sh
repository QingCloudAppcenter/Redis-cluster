cp ./toml/* /etc/confd/conf.d/
cp ./tmpl/* /etc/confd/templates/

chmod +x ./stop-redis-server.sh
cp ./stop-redis-server.sh /opt/redis/bin/stop-redis-server.sh

chmod +x ./restart-redis-server.sh
cp ./restart-redis-server.sh /opt/redis/bin/restart-redis-server.sh

chmod +x ./redis-cluster.py
cp ./redis-cluster.py /opt/redis/bin/redis-cluster.py

chmod +x ./monitor.py
cp ./monitor.py /opt/redis/bin/monitor.py
