from datetime import datetime

from celery.decorators import task
from celery import chain

from atmosphere.logger import logger

from core.email import send_instance_email

def get_driver(driverCls, provider, identity):
    logger.debug("getting driver...")
    from service import compute
    compute.initialize()
    driver = driverCls(provider, identity)
    if driver:
        logger.debug("created driver.")
        return driver

@task(name="deploy_to", max_retries=2, default_retry_delay=120, ignore_result=True)
def deploy_to(driverCls, provider, identity, instance, *args, **kwargs):
    try:
        logger.debug("deploy_to task started at %s." % datetime.now())
        driver = get_driver(driverCls, provider, identity)
        driver.deploy_init_to(instance, *args, **kwargs)
        logger.debug("deploy_to task finished at %s." % datetime.now())
    except Exception as exc:
        logger.warn(exc)
        deploy_to.retry(exc=exc)


@task(name="deploy_init_to",
      default_retry_delay=60,
      ignore_result=True,
      max_retries=1)
def deploy_init_to(driverCls, provider, identity, instance_id, *args, **kwargs):
    try:
        logger.debug("deploy_init_to task started at %s." % datetime.now())
        driver = get_driver(driverCls, provider, identity)
        instance = driver.get_instance(instance_id)
        image_metadata = driver._connection.ex_get_image_metadata(instance.machine)
        image_already_deployed = image_metadata.get("not_deployed")
        if not instance.ip and not image_already_deployed:
            logger.debug("Chain -- Floating_ip + deploy_init + email")
            chain(add_floating_ip.si(driverCls,
                                     provider,
                                     identity,
                                     instance_id),
                  _deploy_init_to.si(driverCls,
                                     provider,
                                     identity,
                                     instance_id),
                  _send_instance_email.si(driverCls,
                                          provider,
                                          identity,
                                          instance_id)).apply_async()
        elif not image_already_deployed:
            logger.debug("Chain -- deploy_init + email")
            chain(_deploy_init_to.si(driverCls,
                                     provider,
                                     identity,
                                     instance_id),
                  _send_instance_email.si(driverCls,
                                          provider,
                                          identity,
                                          instance_id)).apply_async()
        elif not instance.ip:
            logger.debug("Chain -- Floating_ip + email")
            chain(add_floating_ip.si(driverCls,
                                     provider,
                                     identity,
                                     instance_id),
                  _send_instance_email.si(driverCls,
                                          provider,
                                          identity,
                                          instance_id)).apply_async()
        else:
            logger.debug("delay -- email")
            _send_instance_email.delay(driverCls,
                                       provider,
                                       identity,
                                       instance_id)
        logger.debug("deploy_init_to task finished at %s." % datetime.now())
    except Exception as exc:
        logger.warn(exc)
        deploy_init_to.retry(exc=exc)


@task(name="_send_instance_email",
      default_retry_delay=120,
      ignore_result=True,
      max_retries=2)
def _send_instance_email(driverCls, provider, identity, instance_id):
    try:
        logger.debug("_send_instance_email task started at %s." % datetime.now())
        driver = get_driver(driverCls, provider, identity)
        instance = driver.get_instance(instance_id)
        username = identity.user.username
        created = datetime.strptime(instance.extra['created'],
                                    "%Y-%m-%dT%H:%M:%SZ")
        send_instance_email(username,
                            instance.id,
                            instance.ip,
                            created,
                            username)
    except Exception as exc:
        logger.warn(exc)
        deploy_init_to.retry(exc=exc)


@task(name="_deploy_init_to",
      default_retry_delay=120,
      ignore_result=True,
      max_retries=2)
def _deploy_init_to(driverCls, provider, identity, instance_id):
    try:
        logger.debug("_deploy_init_to task started at %s." % datetime.now())
        #logger.debug("_deploy_init_to %s" % driverCls)
        #logger.debug("_deploy_init_to %s" % provider)
        #logger.debug("_deploy_init_to %s" % identity)
        #logger.debug("_deploy_init_to %s" % args)
        #logger.debug("_deploy_init_to %s" % kwargs)
        from service import compute
        compute.initialize()
        driver = driverCls(provider, identity)
        instance = driver.get_instance(instance_id)
        if not instance:
            #Breakout if instance is destroyed
            logger.debug("Instance already deleted: %s." % instance_id)
            return
        instance._node.extra['password'] = None
        deployed = driver.deploy_init_to(instance)
        if not deployed:
            _deploy_init_to.retry()
        logger.debug("_deploy_init_to task finished at %s." % datetime.now())
    except Exception as exc:
        logger.warn(exc)
        _deploy_init_to.retry(exc=exc)


@task(name="add_floating_ip",
      default_retry_delay=15,
      ignore_result=True,
      max_retries=6)
def add_floating_ip(driverCls, provider, identity, instance_alias, *args, **kwargs):
    try:
        logger.debug("add_floating_ip task started at %s." % datetime.now())
        from service import compute
        compute.initialize()
        driver = driverCls(provider, identity)
        instance = driver.get_instance(instance_alias)
        if not instance.ip:
            driver._add_floating_ip(instance, *args, **kwargs)
        else:
            logger.debug("public ip already found! %s" % instance.ip)
        logger.debug("add_floating_ip task finished at %s." % datetime.now())
    except Exception as exc:
        add_floating_ip.retry(exc=exc)


@task(name="destroy_instance",
      default_retry_delay=15,
      ignore_result=True,
      max_retries=6)
def destroy_instance(driverCls, provider, identity, instance_alias):
    try:
        logger.debug("destroy_instance task started at %s." % datetime.now())
        from service import compute
        from service.driver import OSDriver
        compute.initialize()
        driver = driverCls(provider, identity)
        logger.debug("Provider identified as %s" % provider)
        instance = driver.get_instance(instance_alias)
        if instance:
            #First disassociate
            if type(driver) == OSDriver:
                logger.debug("OSDriver Logic -- Disassociate floating IP")
                driver._connection.ex_disassociate_floating_ip(instance)
            #Then destroy
            node_destroyed = driver._connection.destroy_node(instance)
        else:
            logger.debug("Instance already deleted: %s." % instance.id)

        if type(driver) == OSDriver:
            #Spawn off the last two tasks
            logger.debug("OSDriver Logic -- Remove floating ips and check"
            " for empty tenant")
            chain(_remove_floating_ip.subtask((driverCls,
                                     provider,
                                     identity), immutable=True, countdown=5),
                  _check_empty_tenant_network.subtask((driverCls,
                                     provider,
                                     identity), immutable=True, countdown=60)
                 ).apply_async()

        logger.debug("destroy_instance task finished at %s." % datetime.now())
        return node_destroyed
    except Exception as exc:
        logger.warn(exc)
        destroy_instance.retry(exc=exc)


@task(name="_check_empty_tenant_network",
      default_retry_delay=60,
      ignore_result=True,
      max_retries=1)
def _check_empty_tenant_network(driverCls, provider, identity, *args, **kwargs):
    try:
        logger.debug("_check_empty_tenant_network task started at %s." % datetime.now())
        from service import compute
        compute.initialize()
        driver = driverCls(provider, identity)
        instances = driver.list_instances()
        active_instances = False
        for instance in instances:
            if driver._is_active_instance(instance):
                active_instances = True
                break
        if not active_instances:
            #Check for tenant network
            from service.accounts.openstack import AccountDriver as\
            OSAccountDriver
            os_driver = OSAccountDriver()
            username = identity.user.username
            tenant_name = username
            logger.info("No active instances. Removing tenant network"
                    "from %s"
                    % username)
            os_driver.network_manager.delete_tenant_network(username,
                    tenant_name)
        logger.debug("_check_empty_tenant_network task finished at %s." % datetime.now())
    except Exception as exc:
        logger.warn(exc)
        _check_empty_tenant_network.retry(exc=exc)

@task(name="_remove_floating_ip",
      default_retry_delay=15,
      ignore_result=True,
      max_retries=6)
def _remove_floating_ip(driverCls, provider, identity, *args, **kwargs):
    try:
        logger.debug("remove_floating_ip task started at %s." % datetime.now())
        from service import compute
        compute.initialize()
        driver = driverCls(provider, identity)
        for f_ip in driver._connection.ex_list_floating_ips():
            if not f_ip.get('instance_id'):
                driver._connection.ex_deallocate_floating_ip(f_ip['id'])
                logger.info("Removed unused Floating IP: %s" % f_ip)
        logger.debug("remove_floating_ip task finished at %s." % datetime.now())
    except Exception as exc:
        logger.warn(exc)
        _remove_floating_ip.retry(exc=exc)