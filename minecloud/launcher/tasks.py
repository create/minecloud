import boto
import boto.ec2
import datetime
import os
import redis
import time

from celery import task
from django.template.loader import render_to_string
from django.utils.timezone import utc

from .models import Instance
from .sseview import send_event

import urllib
import urllib2
import base64
import string

@task
def launch(instance_id):
    # Retrive instance obj from DB.
    instance = Instance.objects.get(pk=instance_id)

    # Set variables to launch EC2 instance
    ec2_ami = os.getenv('MCL_EC2_AMI')
    ec2_region = os.getenv('MCL_EC2_REGION', 'us-west-2')
    ec2_keypair = os.getenv('MCL_EC2_KEYPAIR','MinecraftEC2')
    ec2_instancetype = os.getenv('MCL_EC2_INSTANCE_TYPE', 'm3.medium')
    ec2_secgroups = [os.getenv('MCL_EC2_SECURITY_GROUP', 'minecraft')]

    # ec2_env_vars populate the userdata.txt file. Cloud-init will append
    # them to /etc/environment on the launched EC2 instance during bootup.
    ec2_env_vars = {'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID'),
                    'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY'),
                    'MSM_S3_BUCKET': os.getenv('MSM_S3_BUCKET'),
                    'DATABASE_URL': os.getenv('DATABASE_URL'),
                    'MEMCACHIER_SERVERS': os.getenv('MEMCACHIER_SERVERS'),
                    'MEMCACHIER_USERNAME': os.getenv('MEMCACHIER_USERNAME'),
                    'MEMCACHIER_PASSWORD': os.getenv('MEMCACHIER_PASSWORD'),
                    'REDISTOGO_URL': os.getenv('REDISTOGO_URL'),
                   }
    ec2_userdata = render_to_string('launcher/userdata.txt', ec2_env_vars)

    # Launch EC2 instance
    region = boto.ec2.get_region(ec2_region)
    conn = boto.connect_ec2(region=region)
    reservation = conn.run_instances(
                        image_id=ec2_ami,
                        key_name=ec2_keypair,
                        security_groups=ec2_secgroups,
                        instance_type=ec2_instancetype,
                        user_data=ec2_userdata)
    server = reservation.instances[0]
    while server.state == u'pending':
        time.sleep(5)
        server.update()

    # Sometimes there's a delay assigning the ip address.
    while not server.ip_address:
        time.sleep(5)
        server.update()

    # Save to DB and send notification
    instance.name = server.id
    instance.ip_address = server.ip_address
    instance.ami = server.image_id
    instance.state = 'pending'
    instance.save()
    send_event('instance_state', instance.state)

    # Update dynamic IP
    dynamic_ip_hostname = os.getenv('NO_IP_HOSTNAME', None)
    dynamic_ip_username = os.getenv('NO_IP_USERNAME', None)
    dynamic_ip_password = os.getenv('NO_IP_PASSWORD', None)
    if dynamic_ip_hostname and dynamic_ip_username and dynamic_ip_password:
        opener = urllib2.build_opener()
        auth = base64.encodestring('%s:%s' % (dynamic_ip_username, dynamic_ip_password)).replace('\n', '')
        opener.addheaders = [('User-agent', 'Minecloud-No-IP/1.0 http://github.com/toffer/minecloud'), ("Authorization", "Basic %s" % auth)]
        url = "http://dynupdate.no-ip.com/nic/update?hostname=" + urllib.quote_plus(dynamic_ip_hostname) + "&myip=" + urllib.quote_plus(server.ip_address)
        opener.open(url)

    # Send task to check if instance is running
    check_state.delay(instance_id, 'running')

    return True


@task(max_retries=60)
def check_state(instance_id, state):
    instance = Instance.objects.get(pk=instance_id)
    if instance.state == state:
        send_event('instance_state', instance.state)
    # elif instance.state in ['initiating', 'pending', 'killing', 'shutting down']:
    else:
        check_state.retry(countdown=5)


@task
def terminate(instance_id):
    # Send redis message to backup
    redis_url = os.getenv('REDISTOGO_URL')
    conn = redis.StrictRedis.from_url(redis_url)
    conn.publish('command', 'backup')

    # Wait for backup to finish.
    instance = Instance.objects.get(pk=instance_id)
    while instance.state != 'backup finished':
        time.sleep(5)
        instance = Instance.objects.get(pk=instance_id)

    # Update instance state
    instance.state = 'stopping'
    instance.save()
    send_event('instance_state', instance.state)

    # Shut down, then terminate instance
    ec2_region = os.getenv('MCL_EC2_REGION', 'us-west-2')
    region = boto.ec2.get_region(ec2_region)
    conn = boto.connect_ec2(region=region)
    results = conn.stop_instances(instance_ids=[instance.name])
    mc_server = results[0]
    mc_server.update()
    while mc_server.state != u'stopped':
        time.sleep(10)
        mc_server.update()
    conn.terminate_instances(instance_ids=[instance.name])

    # Save to DB and send notification
    # But first, refresh instance, since state has changed.
    instance = Instance.objects.get(pk=instance_id)
    timestamp = datetime.datetime.utcnow().replace(tzinfo=utc)
    instance.end = timestamp
    instance.save()

    # Send task to check if instance has been terminated.
    check_state.delay(instance_id, 'terminated')

    return True
