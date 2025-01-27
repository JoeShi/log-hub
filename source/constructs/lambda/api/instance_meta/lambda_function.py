# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import uuid
import urllib3
from datetime import datetime
import concurrent.futures

import boto3
import time
from botocore import config
from aws_svc_mgr import (SvcManager, Boto3API, DocumentType)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

solution_version = os.environ.get("SOLUTION_VERSION", "v1.0.0")
solution_id = os.environ.get("SOLUTION_ID", "SO8025")
user_agent_config = {
    "user_agent_extra": f"AwsSolution/{solution_id}/{solution_version}"
}
default_config = config.Config(**user_agent_config)

# Get DDB resource.
dynamodb = boto3.resource('dynamodb', config=default_config)

instance_meta_table_name = os.environ.get('INSTANCEMETA_TABLE')
agent_status_table_name = os.environ.get('AGENTSTATUS_TABLE')
instance_meta_table = dynamodb.Table(instance_meta_table_name)
log_agent_status_table = dynamodb.Table(agent_status_table_name)
default_region = os.environ.get('AWS_REGION')

http = urllib3.PoolManager()

sts = boto3.client("sts", config=default_config)
account_id = sts.get_caller_identity()["Account"]


class APIException(Exception):

    def __init__(self, message):
        self.message = message


def handle_error(func):
    """ Decorator for exception handling """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except APIException as e:
            logger.error(e)
            raise e
        except Exception as e:
            logger.error(e)
            raise RuntimeError(
                'Unknown exception, please check Lambda log for more details')

    return wrapper


@handle_error
def lambda_handler(event, context):
    #logger.info("Received event: " + json.dumps(event, indent=2))

    action = event['info']['fieldName']
    args = event['arguments']

    if action == "createInstanceMeta":
        return create_instance_meta(**args)
    elif action == "updateInstanceMeta":
        return update_instance_meta(**args)
    elif action == "listInstances":
        return list_instances(**args)
    elif action == "getLogAgentStatus":
        return get_log_agent_status(**args)
    elif action == "requestInstallLogAgent":
        return request_install_log_agent(**args)
    else:
        logger.info('Event received: ' + json.dumps(event, indent=2))
        raise RuntimeError(f'Unknown action {action}')


def create_instance_meta(**args):
    """  Create a InstanceMeta """
    logger.info('create InstanceMeta')
    id = str(uuid.uuid4())
    instance_meta_table.put_item(
        Item={
            'id': id,
            'logAgent': args['logAgent'],
            'instanceId': args['instanceId'],
            'appPipelineId': args['appPipelineId'],
            'confId': args['confId'],
            'groupId': args['groupId'],
            'createdDt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'status': 'ACTIVE',
        })
    return id


def create_log_agent_status(instance_id,
                            command_id,
                            status,
                            accountId=account_id,
                            region=default_region):
    """  Create a LogAgentStatus """
    logger.info('create LogAgentStatus')
    log_agent_status_table.put_item(
        Item={
            'instanceId': instance_id,
            'id': command_id,
            'accountId': accountId,
            'region': region,
            'createDt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'status': status,
            'updatedDt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        })
    return id


def list_instances(maxResults=10,
                   nextToken='',
                   instanceSet=set(),
                   tags=list(),
                   accountId=account_id,
                   region=default_region):
    """  List instance from ssm describe_instance_information API """
    max_results = maxResults  # args['maxResults']
    next_token = nextToken  # args['nextToken']
    instance_set = instanceSet  # args['instanceSet']
    if max_results > 50 or max_results < 5:
        raise APIException(
            'maxResults have to be more than 4 or less than or equal to 50')

    logger.info(
        f'List Instances from Boto3 API in MaxResults {max_results} with {next_token}, the InstanceSet is {instance_set}'
    )
    filters = []
    if tags:
        filters = tags
    else:
        filters = [{
            'Key': 'PingStatus',
            'Values': ['Online']
        }, {
            'Key': 'PlatformTypes',
            'Values': ['Linux']
        }]
        if instance_set:
            filter_instance_ids = {
                'Key': 'InstanceIds',
                'Values': instance_set
            }
            filters.append(filter_instance_ids)
    try:
        # Get SSM resource
        svcMgr = SvcManager()
        ssm = svcMgr.get_client(sub_account_id=accountId,
                                service_name='ssm',
                                type=Boto3API.CLIENT,
                                region=region)
        resp = ssm.describe_instance_information(Filters=filters,
                                                 MaxResults=max_results,
                                                 NextToken=next_token)
    except Exception as e:
        err_message = str(e)
        trimed_message = err_message.split(':', 1)[1]
        raise APIException(trimed_message)

    # Assume all items are returned in the scan request
    instance_information_list = resp['InstanceInformationList']
    instances = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(parse_ssm_instance_info,
                            instance_info,
                            accountId=accountId,
                            region=region)
            for instance_info in instance_information_list
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                instance = future.result()
                instances.append(instance)
            except Exception as e:
                raise APIException(e)

    if 'NextToken' in resp:
        next_token = resp['NextToken']
    else:
        next_token = ""
    return {
        'nextToken': next_token,
        'instances': instances,
    }


def parse_ssm_instance_info(instance_info, accountId, region=default_region):
    instance = {}
    instance['id'] = instance_info['InstanceId']
    instance['platformName'] = instance_info['PlatformName']
    instance['ipAddress'] = instance_info['IPAddress']
    instance['computerName'] = instance_info['ComputerName']
    instance['name'] = '-'
    # Get EC2 resource
    svcMgr = SvcManager()
    ec2 = svcMgr.get_client(sub_account_id=accountId,
                            service_name='ec2',
                            type=Boto3API.CLIENT,
                            region=region)
    instance_tags = ec2.describe_tags(Filters=[
        {
            'Name': 'resource-id',
            'Values': [
                instance_info['InstanceId'],
            ],
        },
    ])
    for tag in instance_tags['Tags']:
        if tag['Key'] == 'Name':
            instance['name'] = tag['Value']
            break
    return instance


def get_log_agent_status(instanceId: str,
                         accountId=account_id,
                         region=default_region) -> str:
    """ Get log agent status from dynamodb status table """
    logger.info('Checking instance log agent status')
    agentStatus = check_agent_status(instance_id=instanceId,
                                     accountId=accountId,
                                     region=region)

    return agentStatus


def request_install_log_agent(
        instanceIdSet=set(), accountId=account_id, region=default_region):
    """ Use SSM SendCommand API to install logging agent """
    logger.info('Run documentation')
    logger.info(instanceIdSet)
    unsuccessInstances = set()
    if len(instanceIdSet) == 0:
        logger.info("Empty instance set input received!")
        return unsuccessInstances
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(instanceIdSet)) as executor:
        futures = [
            executor.submit(single_agent_installation,
                            instanceId,
                            accountId=accountId,
                            region=region) for instanceId in instanceIdSet
        ]
        concurrent.futures.wait(futures)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(instanceIdSet)) as executor:
        futures = [
            executor.submit(update_agent_status,
                            instance_id=instanceId,
                            status="Installing",
                            accountId=accountId,
                            region=region) for instanceId in instanceIdSet
        ]
        concurrent.futures.wait(futures)


def update_instance_meta(**args):
    """ set confIdset, groupIdSet in InstanceMeta table """
    logger.info('Update InstanceMeta Status in DynamoDB')
    id = args['id']
    resp = instance_meta_table.get_item(Key={'id': id})
    if 'Item' not in resp:
        raise APIException('InstanceMeta Not Found')

    instance_meta_table.update_item(
        Key={'id': id},
        UpdateExpression=
        'SET #confIdset = :cset, #groupIdset = :gset, #updatedDt= :uDt',
        ExpressionAttributeNames={
            '#confIdset': 'confIdset',
            '#groupIdset': 'groupIdset',
            '#updatedDt': 'updatedDt'
        },
        ExpressionAttributeValues={
            ':cset': set(args['confIdset']),
            ':gset': set(args['groupIdset']),
            ':uDt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        })


def check_agent_status(instance_id,
                       accountId=account_id,
                       region=default_region):
    """ check agent status in ddb table """
    logger.info('Checking LogAgentStatus Status in DynamoDB')
    resp = log_agent_status_table.get_item(Key={'instanceId': instance_id})
    if 'Item' not in resp:
        logger.info("InstanceId: " + instance_id +
                    " not found in table, creating empty record!")
        create_log_agent_status(instance_id, "Empty_Command_Id",
                                "Not_Installed", accountId, region)
        return "Not_Installed"
    logger.info(resp)
    return resp['Item']['status']


def get_agent_commandId(instance_id,
                        accountId=account_id,
                        region=default_region):
    """ check agent commandId in ddb table """
    logger.info('Getting agent commandId in DynamoDB for instance: ' +
                instance_id)
    resp = log_agent_status_table.get_item(Key={'instanceId': instance_id})
    if 'Item' not in resp:
        logger.info("InstanceId: " + instance_id +
                    " not found in table, creating empty record!")

        create_log_agent_status(instance_id=instance_id,
                                command_id="Empty_Command_Id",
                                status="Not_Installed",
                                accountId=accountId,
                                region=region)
        return "Not_Installed"
    logger.info(resp)
    return resp['Item']['id']


def update_agent_status(instance_id,
                        status,
                        accountId=account_id,
                        region=default_region):
    """ set agent status and update date in LogAgentStatus"""
    agentStatus = check_agent_status(instance_id=instance_id,
                                     accountId=accountId,
                                     region=region)
    logger.info("Previous agent status for: " + instance_id + " is: " +
                agentStatus)
    logger.info("Change status to: " + status)
    logger.info('Updating LogAgentStatus Status in DynamoDB')

    log_agent_status_table.update_item(
        Key={'instanceId': instance_id},
        UpdateExpression='SET #status = :sset, #updatedDt = :uDt',
        ExpressionAttributeNames={
            '#status': 'status',
            '#updatedDt': 'updatedDt',
        },
        ExpressionAttributeValues={
            ':sset': status,
            ':uDt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        })
    return "Updated"


def single_agent_installation(instance_id,
                              accountId=account_id,
                              region=default_region):
    """ retry unsuccess installation"""
    agentStatus = check_agent_status(instance_id=instance_id,
                                     accountId=accountId,
                                     region=region)
    if agentStatus == "Online":
        return
    else:
        # Get EC2 resource
        svcMgr = SvcManager()
        ec2 = svcMgr.get_client(sub_account_id=accountId,
                                service_name='ec2',
                                type=Boto3API.CLIENT,
                                region=region)
        describeInstanceResponse = ec2.describe_instances(
            InstanceIds=[instance_id])
        reservations = describeInstanceResponse['Reservations'][0]
        instances = reservations['Instances'][0]
        architecture = instances['Architecture']
        filter_instance_ids = {'Key': 'InstanceIds', 'Values': [instance_id]}

        #Get SSM resource
        ssm = svcMgr.get_client(sub_account_id=accountId,
                                service_name='ssm',
                                type=Boto3API.CLIENT,
                                region=region)
        logger.info('ready for installation')
        describeInstanceInfoResponse = ssm.describe_instance_information(
            Filters=[filter_instance_ids], )
        instance_information_list = describeInstanceInfoResponse[
            'InstanceInformationList']
        platform_name = instance_information_list[0]['PlatformName']
        systemd_location = "/usr/lib"
        arch_append = ""
        if architecture == "arm64":
            arch_append = "-arm64"
        if platform_name == "Ubuntu":
            systemd_location = "/etc"
        document_name = svcMgr.get_document_name(
            sub_account_id=accountId,
            type=DocumentType.AGENT_INSTALL,
            region=region)
        logger.info(f'document_name is {document_name}')
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=document_name,
            Parameters={
                'ARCHITECTURE': [arch_append],
                'SYSTEMDPATH': [systemd_location]
            },
        )
        logger.info('Successfully triggered installation')
        command = response['Command']
        commandId = command['CommandId']
        create_log_agent_status(instance_id=instance_id,
                                command_id=commandId,
                                status="Installing",
                                accountId=accountId,
                                region=region)


def agent_health_check(
        instanceIdSet=set(), accountId=account_id, region=default_region):
    unsuccessInstances = set()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(instanceIdSet)) as executor:
        futures = [
            executor.submit(get_instance_invocation,
                            instanceId=instanceId,
                            accountId=accountId,
                            region=region) for instanceId in instanceIdSet
        ]
        concurrent.futures.wait(futures)
    try:
        """send command to check agent status"""
        svcMgr = SvcManager()
        ssm = svcMgr.get_client(sub_account_id=accountId,
                                service_name='ssm',
                                type=Boto3API.CLIENT,
                                region=region)
        response = ssm.send_command(
            InstanceIds=list(instanceIdSet),
            DocumentName="AWS-RunShellScript",
            Parameters={
                'commands': ['curl -s http://127.0.0.1:2022/api/v1/health']
            },
        )
        command_id = response['Command']['CommandId']
    except Exception as e:
        logger.error(e)
        return unsuccessInstances
    time.sleep(5)
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(instanceIdSet)) as executor:
            futures = [
                executor.submit(get_agent_health_check_output,
                                command_id,
                                instanceId,
                                unsuccessInstances,
                                accountId=account_id,
                                region=default_region)
                for instanceId in instanceIdSet
            ]
            concurrent.futures.wait(futures)
    except Exception as e:
        logger.error(e)
        return unsuccessInstances
    return unsuccessInstances


def get_instance_invocation(instanceId,
                            accountId=account_id,
                            region=default_region):
    """check command output"""
    commandId = get_agent_commandId(instanceId,
                                    accountId=accountId,
                                    region=region)
    if commandId == "Empty_Command_Id":
        return "Not_Installed"

    svcMgr = SvcManager()
    ssm = svcMgr.get_client(sub_account_id=accountId,
                            service_name='ssm',
                            type=Boto3API.CLIENT,
                            region=region)
    response = ssm.get_command_invocation(CommandId=commandId,
                                          InstanceId=instanceId)
    commandStatus = response['Status']
    logger.info("InstanceId: " + instanceId + " Command Status: " +
                commandStatus)
    if commandStatus == "TimedOut" or commandStatus == "Cancelled":
        update_agent_status(instance_id=instanceId,
                            status="Offline",
                            accountId=account_id,
                            region=default_region)
    return response


def get_agent_health_check_output(command_id,
                                  instanceId,
                                  unsuccessInstances=set(),
                                  accountId=account_id,
                                  region=default_region):
    svcMgr = SvcManager()
    ssm = svcMgr.get_client(sub_account_id=accountId,
                            service_name='ssm',
                            type=Boto3API.CLIENT,
                            region=region)
    output = ssm.get_command_invocation(
        CommandId=command_id,
        InstanceId=instanceId,
    )
    if (len(output['StandardOutputContent']) >
            0) and ('fluent-bit' in output['StandardOutputContent']):
        logger.info("Instance %s is Online" % instanceId)
        update_agent_status(instance_id=instanceId,
                            status="Online",
                            accountId=accountId,
                            region=region)
    else:
        logger.info("Instance %s is Offline" % instanceId)
        update_agent_status(instance_id=instanceId,
                            status="Offline",
                            accountId=accountId,
                            region=region)

        unsuccessInstances.add(instanceId)
