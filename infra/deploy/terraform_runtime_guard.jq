# terraform_runtime_guard.sh から共用する ECS task definition 正規化。
# AWS describe-task-definition と Terraform plan JSON の表現差だけを吸収し、
# runtime の意味が変わる属性は同じ canonical object に残して比較する。

def guard_sort_objects:
  sort_by(tojson);

def guard_norm_port_mapping:
  {
    container_port: (.containerPort // 0),
    host_port: (.hostPort // .containerPort // 0),
    protocol: (.protocol // "tcp"),
    name: (.name // ""),
    app_protocol: (.appProtocol // "")
  };

def guard_norm_health_check:
  if . == null then null
  else {
    command: (.command // []),
    interval: (.interval // 30),
    timeout: (.timeout // 5),
    retries: (.retries // 3),
    start_period: (.startPeriod // 0)
  }
  end;

def guard_norm_log_configuration:
  if . == null then null
  else {
    log_driver: (.logDriver // ""),
    options: (.options // {}),
    secret_options: ((.secretOptions // []) |
      map({name: (.name // ""), value_from: (.valueFrom // "")}) |
      guard_sort_objects)
  }
  end;

def guard_norm_container:
  . as $container |
  {
    name: ($container.name // ""),
    cpu: ($container.cpu // 0),
    memory: ($container.memory // 0),
    memory_reservation: ($container.memoryReservation // 0),
    essential: (if ($container | has("essential")) then $container.essential else true end),
    entry_point: ($container.entryPoint // []),
    command: ($container.command // []),
    working_directory: ($container.workingDirectory // ""),
    port_mappings: (($container.portMappings // []) |
      map(guard_norm_port_mapping) | guard_sort_objects),
    health_check: (($container.healthCheck // null) | guard_norm_health_check),
    log_configuration: (($container.logConfiguration // null) |
      guard_norm_log_configuration),
    mount_points: (($container.mountPoints // []) |
      map({
        source_volume: (.sourceVolume // ""),
        container_path: (.containerPath // ""),
        read_only: (.readOnly // false)
      }) | guard_sort_objects),
    volumes_from: (($container.volumesFrom // []) |
      map({source_container: (.sourceContainer // ""), read_only: (.readOnly // false)}) |
      guard_sort_objects),
    depends_on: (($container.dependsOn // []) |
      map({container_name: (.containerName // ""), condition: (.condition // "")}) |
      guard_sort_objects),
    environment_files: (($container.environmentFiles // []) |
      map({value: (.value // ""), type: (.type // "")}) | guard_sort_objects),
    start_timeout: ($container.startTimeout // 0),
    stop_timeout: ($container.stopTimeout // 0),
    hostname: ($container.hostname // ""),
    user: ($container.user // ""),
    privileged: ($container.privileged // false),
    readonly_root_filesystem: ($container.readonlyRootFilesystem // false),
    interactive: ($container.interactive // false),
    pseudo_terminal: ($container.pseudoTerminal // false),
    disable_networking: ($container.disableNetworking // false),
    dns_servers: (($container.dnsServers // []) | sort),
    dns_search_domains: (($container.dnsSearchDomains // []) | sort),
    docker_labels: ($container.dockerLabels // {}),
    docker_security_options: (($container.dockerSecurityOptions // []) | sort),
    extra_hosts: (($container.extraHosts // []) |
      map({hostname: (.hostname // ""), ip_address: (.ipAddress // "")}) |
      guard_sort_objects),
    links: (($container.links // []) | sort),
    linux_parameters: ($container.linuxParameters // null),
    system_controls: (($container.systemControls // []) |
      map({namespace: (.namespace // ""), value: (.value // "")}) |
      guard_sort_objects),
    ulimits: (($container.ulimits // []) |
      map({name: (.name // ""), soft_limit: (.softLimit // 0), hard_limit: (.hardLimit // 0)}) |
      guard_sort_objects),
    resource_requirements: (($container.resourceRequirements // []) |
      map({type: (.type // ""), value: (.value // "")}) | guard_sort_objects),
    firelens_configuration: ($container.firelensConfiguration // null),
    repository_credentials: ($container.repositoryCredentials // null),
    credential_specs: (($container.credentialSpecs // []) | sort),
    restart_policy: (
      if $container.restartPolicy == null then null
      else {
        enabled: ($container.restartPolicy.enabled // false),
        ignored_exit_codes: (($container.restartPolicy.ignoredExitCodes // []) | sort),
        restart_attempt_period: ($container.restartPolicy.restartAttemptPeriod // 0)
      }
      end),
    version_consistency: ($container.versionConsistency // "")
  };

def guard_norm_aws_volume:
  . as $volume |
  {
    name: ($volume.name // ""),
    configure_at_launch: ($volume.configuredAtLaunch // false),
    host_path: ($volume.host.sourcePath // ""),
    docker_volume_configuration: (
      if $volume.dockerVolumeConfiguration == null then null
      else {
        scope: ($volume.dockerVolumeConfiguration.scope // ""),
        autoprovision: ($volume.dockerVolumeConfiguration.autoprovision // false),
        driver: ($volume.dockerVolumeConfiguration.driver // ""),
        driver_opts: ($volume.dockerVolumeConfiguration.driverOpts // {}),
        labels: ($volume.dockerVolumeConfiguration.labels // {})
      }
      end),
    efs_volume_configuration: (
      if $volume.efsVolumeConfiguration == null then null
      else {
        file_system_id: ($volume.efsVolumeConfiguration.fileSystemId // ""),
        root_directory: ($volume.efsVolumeConfiguration.rootDirectory // ""),
        transit_encryption: ($volume.efsVolumeConfiguration.transitEncryption // "DISABLED"),
        transit_encryption_port: ($volume.efsVolumeConfiguration.transitEncryptionPort // 0),
        authorization_config: {
          access_point_id: ($volume.efsVolumeConfiguration.authorizationConfig.accessPointId // ""),
          iam: ($volume.efsVolumeConfiguration.authorizationConfig.iam // "DISABLED")
        }
      }
      end),
    fsx_windows_file_server_volume_configuration: (
      if $volume.fsxWindowsFileServerVolumeConfiguration == null then null
      else {
        file_system_id: ($volume.fsxWindowsFileServerVolumeConfiguration.fileSystemId // ""),
        root_directory: ($volume.fsxWindowsFileServerVolumeConfiguration.rootDirectory // ""),
        authorization_config: {
          credentials_parameter: ($volume.fsxWindowsFileServerVolumeConfiguration.authorizationConfig.credentialsParameter // ""),
          domain: ($volume.fsxWindowsFileServerVolumeConfiguration.authorizationConfig.domain // "")
        }
      }
      end)
  };

def guard_norm_tf_volume:
  . as $volume |
  {
    name: ($volume.name // ""),
    configure_at_launch: ($volume.configure_at_launch // false),
    host_path: ($volume.host_path // ""),
    docker_volume_configuration: (
      if (($volume.docker_volume_configuration // []) | length) == 0 then null
      else ($volume.docker_volume_configuration[0] | {
        scope: (.scope // ""),
        autoprovision: (.autoprovision // false),
        driver: (.driver // ""),
        driver_opts: (.driver_opts // {}),
        labels: (.labels // {})
      })
      end),
    efs_volume_configuration: (
      if (($volume.efs_volume_configuration // []) | length) == 0 then null
      else ($volume.efs_volume_configuration[0] | {
        file_system_id: (.file_system_id // ""),
        root_directory: (.root_directory // ""),
        transit_encryption: (.transit_encryption // "DISABLED"),
        transit_encryption_port: (.transit_encryption_port // 0),
        authorization_config: (
          if ((.authorization_config // []) | length) == 0 then
            {access_point_id: "", iam: "DISABLED"}
          else (.authorization_config[0] | {
            access_point_id: (.access_point_id // ""),
            iam: (.iam // "DISABLED")
          })
          end)
      })
      end),
    fsx_windows_file_server_volume_configuration: (
      if (($volume.fsx_windows_file_server_volume_configuration // []) | length) == 0 then null
      else ($volume.fsx_windows_file_server_volume_configuration[0] | {
        file_system_id: (.file_system_id // ""),
        root_directory: (.root_directory // ""),
        authorization_config: (.authorization_config[0] | {
          credentials_parameter: (.credentials_parameter // ""),
          domain: (.domain // "")
        })
      })
      end)
  };

def guard_norm_aws_proxy:
  if . == null then null
  else {
    type: (.type // "APPMESH"),
    container_name: (.containerName // ""),
    properties: ((.properties // []) | map({key: .name, value: .value}) | from_entries)
  }
  end;

def guard_norm_tf_proxy:
  if (. // [] | length) == 0 then null
  else .[0] | {
    type: (.type // "APPMESH"),
    container_name: (.container_name // ""),
    properties: (.properties // {})
  }
  end;

def guard_task_from_aws:
  . as $task |
  {
    family: ($task.family // ""),
    task_role_arn: ($task.taskRoleArn // ""),
    execution_role_arn: ($task.executionRoleArn // ""),
    cpu: (($task.cpu // "") | tostring),
    memory: (($task.memory // "") | tostring),
    network_mode: ($task.networkMode // ""),
    requires_compatibilities: (($task.requiresCompatibilities // []) | sort),
    runtime_platform: {
      cpu_architecture: ($task.runtimePlatform.cpuArchitecture // ""),
      operating_system_family: ($task.runtimePlatform.operatingSystemFamily // "")
    },
    ephemeral_storage_gib: ($task.ephemeralStorage.sizeInGiB // 20),
    ipc_mode: ($task.ipcMode // ""),
    pid_mode: ($task.pidMode // ""),
    placement_constraints: (($task.placementConstraints // []) |
      map({type: (.type // ""), expression: (.expression // "")}) |
      guard_sort_objects),
    proxy_configuration: (($task.proxyConfiguration // null) | guard_norm_aws_proxy),
    inference_accelerators: (($task.inferenceAccelerators // []) |
      map({device_name: (.deviceName // ""), device_type: (.deviceType // "")}) |
      guard_sort_objects),
    volumes: (($task.volumes // []) | map(guard_norm_aws_volume) | guard_sort_objects),
    containers: (($task.containerDefinitions // []) |
      map(guard_norm_container) | sort_by(.name)),
    enable_fault_injection: ($task.enableFaultInjection // false),
    track_latest: false,
    skip_destroy: false,
    tags: (($task.tags // []) | map({key: .key, value: .value}) | from_entries)
  };

def guard_task_from_tf:
  . as $task |
  {
    family: ($task.family // ""),
    task_role_arn: ($task.task_role_arn // ""),
    execution_role_arn: ($task.execution_role_arn // ""),
    cpu: (($task.cpu // "") | tostring),
    memory: (($task.memory // "") | tostring),
    network_mode: ($task.network_mode // ""),
    requires_compatibilities: (($task.requires_compatibilities // []) | sort),
    runtime_platform: (
      if (($task.runtime_platform // []) | length) == 0 then
        {cpu_architecture: "", operating_system_family: ""}
      else ($task.runtime_platform[0] | {
        cpu_architecture: (.cpu_architecture // ""),
        operating_system_family: (.operating_system_family // "")
      })
      end),
    ephemeral_storage_gib: (
      if (($task.ephemeral_storage // []) | length) == 0 then 20
      else ($task.ephemeral_storage[0].size_in_gib // 20)
      end),
    ipc_mode: ($task.ipc_mode // ""),
    pid_mode: ($task.pid_mode // ""),
    placement_constraints: (($task.placement_constraints // []) |
      map({type: (.type // ""), expression: (.expression // "")}) |
      guard_sort_objects),
    proxy_configuration: (($task.proxy_configuration // []) | guard_norm_tf_proxy),
    inference_accelerators: (($task.inference_accelerator // []) |
      map({device_name: (.device_name // ""), device_type: (.device_type // "")}) |
      guard_sort_objects),
    volumes: (($task.volume // []) | map(guard_norm_tf_volume) | guard_sort_objects),
    containers: (($task.container_definitions | fromjson) |
      map(guard_norm_container) | sort_by(.name)),
    enable_fault_injection: ($task.enable_fault_injection // false),
    track_latest: ($task.track_latest // false),
    # Provider retention metadata is not part of an ECS task definition and
    # cannot be observed through describe-task-definition. Terraform enforces
    # it separately; canonical runtime parity therefore normalizes it away.
    skip_destroy: false,
    tags: (($task.tags_all // {}) + ($task.tags // {}))
  };

def guard_norm_aws_network:
  (.networkConfiguration.awsvpcConfiguration // null) as $network |
  if $network == null then null
  else {
    assign_public_ip: (($network.assignPublicIp // "DISABLED") == "ENABLED"),
    security_groups: (($network.securityGroups // []) | sort),
    subnets: (($network.subnets // []) | sort)
  }
  end;

def guard_norm_tf_network:
  if ((.network_configuration // []) | length) == 0 then null
  else (.network_configuration[0] | {
    assign_public_ip: (.assign_public_ip // false),
    security_groups: ((.security_groups // []) | sort),
    subnets: ((.subnets // []) | sort)
  })
  end;

def guard_norm_aws_capacity_provider:
  (. // []) | map({
    capacity_provider: (.capacityProvider // ""),
    weight: (.weight // 0),
    base: (.base // 0)
  }) | guard_sort_objects;

def guard_norm_tf_capacity_provider:
  (. // []) | map({
    capacity_provider: (.capacity_provider // ""),
    weight: (.weight // 0),
    base: (.base // 0)
  }) | guard_sort_objects;

def guard_iam_role_path:
  (. // "") |
  if startswith("arn:aws:iam::") then sub("^arn:aws:iam::[0-9]+:role"; "") else . end;

def guard_aws_tags:
  (. // []) | map({key: .key, value: .value}) | from_entries;

def guard_service_from_aws:
  . as $service |
  {
    name: ($service.serviceName // ""),
    cluster: ($service.clusterArn // ""),
    desired_count: ($service.desiredCount // 0),
    launch_type: ($service.launchType // ""),
    capacity_provider_strategy: (($service.capacityProviderStrategy // []) |
      guard_norm_aws_capacity_provider),
    platform_version: ($service.platformVersion // ""),
    availability_zone_rebalancing: ($service.availabilityZoneRebalancing // "DISABLED"),
    deployment_maximum_percent: ($service.deploymentConfiguration.maximumPercent // 200),
    deployment_minimum_healthy_percent: ($service.deploymentConfiguration.minimumHealthyPercent // 100),
    deployment_circuit_breaker: {
      enable: ($service.deploymentConfiguration.deploymentCircuitBreaker.enable // false),
      rollback: ($service.deploymentConfiguration.deploymentCircuitBreaker.rollback // false)
    },
    deployment_controller: ($service.deploymentController.type // "ECS"),
    deployment_strategy: ($service.deploymentConfiguration.strategy // "ROLLING"),
    deployment_bake_time_minutes: ($service.deploymentConfiguration.bakeTimeInMinutes // 0),
    deployment_alarms: {
      alarm_names: (($service.deploymentConfiguration.alarms.alarmNames // []) | sort),
      enable: ($service.deploymentConfiguration.alarms.enable // false),
      rollback: ($service.deploymentConfiguration.alarms.rollback // false)
    },
    network_configuration: ($service | guard_norm_aws_network),
    load_balancers: (($service.loadBalancers // []) | map({
      target_group_arn: (.targetGroupArn // ""),
      load_balancer_name: (.loadBalancerName // ""),
      container_name: (.containerName // ""),
      container_port: (.containerPort // 0)
    }) | guard_sort_objects),
    service_registries: (($service.serviceRegistries // []) | map({
      registry_arn: (.registryArn // ""),
      container_name: (.containerName // ""),
      container_port: (.containerPort // 0),
      port: (.port // 0)
    }) | guard_sort_objects),
    health_check_grace_period_seconds: ($service.healthCheckGracePeriodSeconds // 0),
    iam_role: (($service.roleArn // "") | guard_iam_role_path),
    scheduling_strategy: ($service.schedulingStrategy // "REPLICA"),
    placement_constraints: (($service.placementConstraints // []) | map({
      type: (.type // ""), expression: (.expression // "")
    }) | guard_sort_objects),
    placement_strategy: (($service.placementStrategy // []) | map({
      type: (.type // ""), field: (.field // "")
    }) | guard_sort_objects),
    enable_execute_command: ($service.enableExecuteCommand // false),
    enable_ecs_managed_tags: ($service.enableECSManagedTags // false),
    propagate_tags: ($service.propagateTags // "NONE"),
    tags: (($service.tags // []) | guard_aws_tags),
    service_connect_enabled: (($service.serviceConnectConfiguration.enabled // false)),
    service_connect_configuration_count: (
      if $service.serviceConnectConfiguration == null then 0 else 1 end),
    volume_configuration_count: (($service.volumeConfigurations // []) | length),
    vpc_lattice_configuration_count: (($service.vpcLatticeConfigurations // []) | length),
    force_new_deployment: false,
    wait_for_steady_state: false,
    triggers: {}
  };

def guard_service_from_tf:
  . as $service |
  {
    name: ($service.name // ""),
    cluster: ($service.cluster // ""),
    desired_count: ($service.desired_count // 0),
    launch_type: ($service.launch_type // ""),
    capacity_provider_strategy: (($service.capacity_provider_strategy // []) |
      guard_norm_tf_capacity_provider),
    platform_version: ($service.platform_version // ""),
    availability_zone_rebalancing: ($service.availability_zone_rebalancing // "DISABLED"),
    deployment_maximum_percent: ($service.deployment_maximum_percent // 200),
    deployment_minimum_healthy_percent: ($service.deployment_minimum_healthy_percent // 100),
    deployment_circuit_breaker: (
      if (($service.deployment_circuit_breaker // []) | length) == 0 then
        {enable: false, rollback: false}
      else ($service.deployment_circuit_breaker[0] | {
        enable: (.enable // false), rollback: (.rollback // false)
      })
      end),
    deployment_controller: (
      if (($service.deployment_controller // []) | length) == 0 then "ECS"
      else ($service.deployment_controller[0].type // "ECS")
      end),
    deployment_strategy: "ROLLING",
    deployment_bake_time_minutes: 0,
    deployment_alarms: (
      if (($service.alarms // []) | length) == 0 then
        {alarm_names: [], enable: false, rollback: false}
      else ($service.alarms[0] | {
        alarm_names: ((.alarm_names // []) | sort),
        enable: (.enable // false),
        rollback: (.rollback // false)
      })
      end),
    network_configuration: ($service | guard_norm_tf_network),
    load_balancers: (($service.load_balancer // []) | map({
      target_group_arn: (.target_group_arn // ""),
      load_balancer_name: (.elb_name // ""),
      container_name: (.container_name // ""),
      container_port: (.container_port // 0)
    }) | guard_sort_objects),
    service_registries: (($service.service_registries // []) | map({
      registry_arn: (.registry_arn // ""),
      container_name: (.container_name // ""),
      container_port: (.container_port // 0),
      port: (.port // 0)
    }) | guard_sort_objects),
    health_check_grace_period_seconds: ($service.health_check_grace_period_seconds // 0),
    iam_role: (($service.iam_role // "") | guard_iam_role_path),
    scheduling_strategy: ($service.scheduling_strategy // "REPLICA"),
    placement_constraints: (($service.placement_constraints // []) | map({
      type: (.type // ""), expression: (.expression // "")
    }) | guard_sort_objects),
    placement_strategy: (($service.ordered_placement_strategy // []) | map({
      type: (.type // ""), field: (.field // "")
    }) | guard_sort_objects),
    enable_execute_command: ($service.enable_execute_command // false),
    enable_ecs_managed_tags: ($service.enable_ecs_managed_tags // false),
    propagate_tags: ($service.propagate_tags // "NONE"),
    tags: (($service.tags_all // {}) + ($service.tags // {})),
    service_connect_enabled: (
      if (($service.service_connect_configuration // []) | length) == 0 then false
      else ($service.service_connect_configuration[0].enabled // false)
      end),
    service_connect_configuration_count: (($service.service_connect_configuration // []) | length),
    volume_configuration_count: (($service.volume_configuration // []) | length),
    vpc_lattice_configuration_count: (($service.vpc_lattice_configurations // []) | length),
    force_new_deployment: ($service.force_new_deployment // false),
    wait_for_steady_state: ($service.wait_for_steady_state // false),
    triggers: ($service.triggers // {})
  };

def guard_norm_aws_ecs_target:
  . as $ecs |
  ($ecs.NetworkConfiguration.awsvpcConfiguration // null) as $network |
  {
    task_count: ($ecs.TaskCount // 1),
    launch_type: ($ecs.LaunchType // ""),
    platform_version: ($ecs.PlatformVersion // ""),
    group: ($ecs.Group // ""),
    enable_ecs_managed_tags: ($ecs.EnableECSManagedTags // false),
    enable_execute_command: ($ecs.EnableExecuteCommand // false),
    propagate_tags: ($ecs.PropagateTags // ""),
    tags: ($ecs.Tags // {}),
    capacity_provider_strategy: (($ecs.CapacityProviderStrategy // []) |
      guard_norm_aws_capacity_provider),
    placement_constraints: (($ecs.PlacementConstraints // []) | map({
      type: (.type // ""), expression: (.expression // "")
    }) | guard_sort_objects),
    placement_strategy: (($ecs.PlacementStrategy // []) | map({
      type: (.type // ""), field: (.field // "")
    }) | guard_sort_objects),
    network_configuration: (
      if $network == null then null
      else {
        assign_public_ip: (($network.AssignPublicIp // "DISABLED") == "ENABLED"),
        security_groups: (($network.SecurityGroups // []) | sort),
        subnets: (($network.Subnets // []) | sort)
      }
      end)
  };

def guard_norm_tf_ecs_target:
  . as $ecs |
  {
    task_count: ($ecs.task_count // 1),
    launch_type: ($ecs.launch_type // ""),
    platform_version: ($ecs.platform_version // ""),
    group: ($ecs.group // ""),
    enable_ecs_managed_tags: ($ecs.enable_ecs_managed_tags // false),
    enable_execute_command: ($ecs.enable_execute_command // false),
    propagate_tags: ($ecs.propagate_tags // ""),
    tags: ($ecs.tags // {}),
    capacity_provider_strategy: (($ecs.capacity_provider_strategy // []) |
      guard_norm_tf_capacity_provider),
    placement_constraints: (($ecs.placement_constraint // []) | map({
      type: (.type // ""), expression: (.expression // "")
    }) | guard_sort_objects),
    placement_strategy: (($ecs.ordered_placement_strategy // []) | map({
      type: (.type // ""), field: (.field // "")
    }) | guard_sort_objects),
    network_configuration: ($ecs | guard_norm_tf_network)
  };

def guard_target_from_aws:
  . as $target |
  {
    id: ($target.Id // ""),
    arn: ($target.Arn // ""),
    role_arn: ($target.RoleArn // ""),
    input: ($target.Input // ""),
    input_path: ($target.InputPath // ""),
    input_transformer: ($target.InputTransformer // null),
    retry_policy: {
      maximum_event_age_in_seconds: ($target.RetryPolicy.MaximumEventAgeInSeconds // 0),
      maximum_retry_attempts: ($target.RetryPolicy.MaximumRetryAttempts // 0)
    },
    dead_letter_arn: ($target.DeadLetterConfig.Arn // ""),
    ecs_target: (($target.EcsParameters // {}) | guard_norm_aws_ecs_target)
  };

def guard_target_from_tf:
  . as $target |
  {
    id: ($target.target_id // ""),
    arn: ($target.arn // ""),
    role_arn: ($target.role_arn // ""),
    input: ($target.input // ""),
    input_path: ($target.input_path // ""),
    input_transformer: (
      if (($target.input_transformer // []) | length) == 0 then null
      else $target.input_transformer[0]
      end),
    retry_policy: (
      if (($target.retry_policy // []) | length) == 0 then
        {maximum_event_age_in_seconds: 0, maximum_retry_attempts: 0}
      else ($target.retry_policy[0] | {
        maximum_event_age_in_seconds: (.maximum_event_age_in_seconds // 0),
        maximum_retry_attempts: (.maximum_retry_attempts // 0)
      })
      end),
    dead_letter_arn: (
      if (($target.dead_letter_config // []) | length) == 0 then ""
      else ($target.dead_letter_config[0].arn // "")
      end),
    ecs_target: (($target.ecs_target // [])[0] | guard_norm_tf_ecs_target)
  };

def guard_rule_from_aws:
  {
    name: (.Name // ""),
    arn: (.Arn // ""),
    state: (.State // ""),
    schedule_expression: (.ScheduleExpression // ""),
    event_pattern: (.EventPattern // ""),
    description: (.Description // ""),
    role_arn: (.RoleArn // ""),
    event_bus_name: (.EventBusName // "default")
  };

def guard_rule_from_tf:
  {
    name: (.name // ""),
    arn: (.arn // ""),
    state: (.state // ""),
    schedule_expression: (.schedule_expression // ""),
    event_pattern: (.event_pattern // ""),
    description: (.description // ""),
    role_arn: (.role_arn // ""),
    event_bus_name: (.event_bus_name // "default")
  };

# Lambda dispatcher configuration. Operational fields such as LastModified and
# LastUpdateStatus are checked separately by the snapshotter; every deployable
# configuration field used by the two ZIP dispatchers is retained here.
def guard_norm_aws_lambda_vpc:
  if . == null or (((.SubnetIds // []) | length) == 0 and
                   ((.SecurityGroupIds // []) | length) == 0) then null
  else {
    subnet_ids: ((.SubnetIds // []) | sort),
    security_group_ids: ((.SecurityGroupIds // []) | sort),
    ipv6_allowed_for_dual_stack: (.Ipv6AllowedForDualStack // false)
  }
  end;

def guard_norm_tf_lambda_vpc:
  if ((. // []) | length) == 0 then null
  else .[0] | {
    subnet_ids: ((.subnet_ids // []) | sort),
    security_group_ids: ((.security_group_ids // []) | sort),
    ipv6_allowed_for_dual_stack: (.ipv6_allowed_for_dual_stack // false)
  }
  end;

def guard_norm_aws_lambda_logging($function_name):
  . // {} | {
    application_log_level: (.ApplicationLogLevel // ""),
    log_format: (.LogFormat // "Text"),
    log_group: (.LogGroup // ("/aws/lambda/" + $function_name)),
    system_log_level: (.SystemLogLevel // "")
  };

def guard_norm_tf_lambda_logging($function_name):
  if ((. // []) | length) == 0 then {
    application_log_level: "",
    log_format: "Text",
    log_group: ("/aws/lambda/" + $function_name),
    system_log_level: ""
  }
  else .[0] | {
    application_log_level: (.application_log_level // ""),
    log_format: (.log_format // "Text"),
    log_group: (.log_group // ("/aws/lambda/" + $function_name)),
    system_log_level: (.system_log_level // "")
  }
  end;

def guard_lambda_from_aws($concurrency; $tags):
  . as $lambda |
  {
    function_name: ($lambda.FunctionName // ""),
    function_arn: ($lambda.FunctionArn // ""),
    role: ($lambda.Role // ""),
    runtime: ($lambda.Runtime // ""),
    handler: ($lambda.Handler // ""),
    architectures: (($lambda.Architectures // ["x86_64"]) | sort),
    code_sha256: ($lambda.CodeSha256 // ""),
    code_signing_config_arn: ($lambda.CodeSigningConfigArn // ""),
    description: ($lambda.Description // ""),
    timeout: ($lambda.Timeout // 3),
    memory_size: ($lambda.MemorySize // 128),
    package_type: ($lambda.PackageType // "Zip"),
    environment: ($lambda.Environment.Variables // {}),
    kms_key_arn: ($lambda.KMSKeyArn // ""),
    dead_letter_target_arn: ($lambda.DeadLetterConfig.TargetArn // ""),
    tracing_mode: ($lambda.TracingConfig.Mode // "PassThrough"),
    layers: (($lambda.Layers // []) | map(.Arn) | sort),
    file_system_configs: (($lambda.FileSystemConfigs // []) | map({
      arn: (.Arn // ""), local_mount_path: (.LocalMountPath // "")
    }) | guard_sort_objects),
    vpc_config: (($lambda.VpcConfig // null) | guard_norm_aws_lambda_vpc),
    ephemeral_storage_size: ($lambda.EphemeralStorage.Size // 512),
    snap_start_apply_on: ($lambda.SnapStart.ApplyOn // "None"),
    logging_config: (($lambda.LoggingConfig // {}) |
      guard_norm_aws_lambda_logging($lambda.FunctionName // "")),
    image_config: {
      entry_point: (($lambda.ImageConfigResponse.ImageConfig.EntryPoint // []) | .),
      command: (($lambda.ImageConfigResponse.ImageConfig.Command // []) | .),
      working_directory: ($lambda.ImageConfigResponse.ImageConfig.WorkingDirectory // "")
    },
    reserved_concurrent_executions: ($concurrency.ReservedConcurrentExecutions // -1),
    tags: ($tags.Tags // {}),
    publish: false,
    skip_destroy: false,
    replace_security_groups_on_destroy: false,
    replacement_security_group_ids: []
  };

def guard_lambda_from_tf:
  . as $lambda |
  {
    function_name: ($lambda.function_name // ""),
    function_arn: ($lambda.arn // ""),
    role: ($lambda.role // ""),
    runtime: ($lambda.runtime // ""),
    handler: ($lambda.handler // ""),
    architectures: (($lambda.architectures // ["x86_64"]) | sort),
    code_sha256: ($lambda.source_code_hash // $lambda.code_sha256 // ""),
    code_signing_config_arn: ($lambda.code_signing_config_arn // ""),
    description: ($lambda.description // ""),
    timeout: ($lambda.timeout // 3),
    memory_size: ($lambda.memory_size // 128),
    package_type: ($lambda.package_type // "Zip"),
    environment: (
      if (($lambda.environment // []) | length) == 0 then {}
      else ($lambda.environment[0].variables // {})
      end),
    kms_key_arn: ($lambda.kms_key_arn // ""),
    dead_letter_target_arn: (
      if (($lambda.dead_letter_config // []) | length) == 0 then ""
      else ($lambda.dead_letter_config[0].target_arn // "")
      end),
    tracing_mode: (
      if (($lambda.tracing_config // []) | length) == 0 then "PassThrough"
      else ($lambda.tracing_config[0].mode // "PassThrough")
      end),
    layers: (($lambda.layers // []) | sort),
    file_system_configs: (($lambda.file_system_config // []) | map({
      arn: (.arn // ""), local_mount_path: (.local_mount_path // "")
    }) | guard_sort_objects),
    vpc_config: (($lambda.vpc_config // []) | guard_norm_tf_lambda_vpc),
    ephemeral_storage_size: (
      if (($lambda.ephemeral_storage // []) | length) == 0 then 512
      else ($lambda.ephemeral_storage[0].size // 512)
      end),
    snap_start_apply_on: (
      if (($lambda.snap_start // []) | length) == 0 then "None"
      else ($lambda.snap_start[0].apply_on // "None")
      end),
    logging_config: (($lambda.logging_config // []) |
      guard_norm_tf_lambda_logging($lambda.function_name // "")),
    image_config: (
      if (($lambda.image_config // []) | length) == 0 then
        {entry_point: [], command: [], working_directory: ""}
      else ($lambda.image_config[0] | {
        entry_point: (.entry_point // []),
        command: (.command // []),
        working_directory: (.working_directory // "")
      })
      end),
    reserved_concurrent_executions: ($lambda.reserved_concurrent_executions // -1),
    tags: (($lambda.tags_all // {}) + ($lambda.tags // {})),
    publish: ($lambda.publish // false),
    skip_destroy: ($lambda.skip_destroy // false),
    replace_security_groups_on_destroy: ($lambda.replace_security_groups_on_destroy // false),
    replacement_security_group_ids: (($lambda.replacement_security_group_ids // []) | sort)
  };

def guard_norm_aws_mapping_destination:
  . // {} | {
    on_failure: (.OnFailure.Destination // ""),
    on_success: (.OnSuccess.Destination // "")
  };

def guard_norm_tf_mapping_destination:
  if ((. // []) | length) == 0 then {on_failure: "", on_success: ""}
  else .[0] | {
    on_failure: (
      if ((.on_failure // []) | length) == 0 then ""
      else (.on_failure[0].destination_arn // "")
      end),
    on_success: (
      if ((.on_success // []) | length) == 0 then ""
      else (.on_success[0].destination_arn // "")
      end)
  }
  end;

def guard_mapping_from_aws($tags):
  . as $mapping |
  {
    arn: ($mapping.EventSourceMappingArn // ""),
    uuid: ($mapping.UUID // ""),
    enabled: (($mapping.State // "") == "Enabled"),
    event_source_arn: ($mapping.EventSourceArn // ""),
    function_arn: ($mapping.FunctionArn // ""),
    batch_size: ($mapping.BatchSize // 100),
    maximum_batching_window_in_seconds: ($mapping.MaximumBatchingWindowInSeconds // 0),
    bisect_batch_on_function_error: ($mapping.BisectBatchOnFunctionError // false),
    maximum_record_age_in_seconds: ($mapping.MaximumRecordAgeInSeconds // 0),
    maximum_retry_attempts: ($mapping.MaximumRetryAttempts // 0),
    parallelization_factor: ($mapping.ParallelizationFactor // 0),
    tumbling_window_in_seconds: ($mapping.TumblingWindowInSeconds // 0),
    starting_position: ($mapping.StartingPosition // ""),
    starting_position_timestamp: (($mapping.StartingPositionTimestamp // "") | tostring),
    function_response_types: (($mapping.FunctionResponseTypes // []) | sort),
    kms_key_arn: ($mapping.KMSKeyArn // ""),
    destination_config: (($mapping.DestinationConfig // {}) |
      guard_norm_aws_mapping_destination),
    filter_patterns: (($mapping.FilterCriteria.Filters // []) |
      map(.Pattern // "") | sort),
    scaling_maximum_concurrency: ($mapping.ScalingConfig.MaximumConcurrency // 0),
    metrics: (($mapping.MetricsConfig.Metrics // []) | sort),
    provisioned_poller: {
      minimum: ($mapping.ProvisionedPollerConfig.MinimumPollers // 0),
      maximum: ($mapping.ProvisionedPollerConfig.MaximumPollers // 0)
    },
    source_access_configurations: (($mapping.SourceAccessConfigurations // []) |
      map({type: (.Type // ""), uri: (.URI // "")}) | guard_sort_objects),
    non_sqs_source_config_count: ([
      $mapping.AmazonManagedKafkaEventSourceConfig,
      $mapping.DocumentDBEventSourceConfig,
      $mapping.SelfManagedEventSource,
      $mapping.SelfManagedKafkaEventSourceConfig
    ] | map(select(. != null)) | length),
    queues: [],
    topics: [],
    tags: ($tags.Tags // {})
  };

def guard_mapping_from_tf:
  . as $mapping |
  {
    arn: ($mapping.arn // ""),
    uuid: ($mapping.uuid // $mapping.id // ""),
    enabled: (if ($mapping | has("enabled")) then $mapping.enabled else true end),
    event_source_arn: ($mapping.event_source_arn // ""),
    function_arn: ($mapping.function_arn // $mapping.function_name // ""),
    batch_size: ($mapping.batch_size // 100),
    maximum_batching_window_in_seconds: ($mapping.maximum_batching_window_in_seconds // 0),
    bisect_batch_on_function_error: ($mapping.bisect_batch_on_function_error // false),
    maximum_record_age_in_seconds: ($mapping.maximum_record_age_in_seconds // 0),
    maximum_retry_attempts: ($mapping.maximum_retry_attempts // 0),
    parallelization_factor: ($mapping.parallelization_factor // 0),
    tumbling_window_in_seconds: ($mapping.tumbling_window_in_seconds // 0),
    starting_position: ($mapping.starting_position // ""),
    starting_position_timestamp: (($mapping.starting_position_timestamp // "") | tostring),
    function_response_types: (($mapping.function_response_types // []) | sort),
    kms_key_arn: ($mapping.kms_key_arn // ""),
    destination_config: (($mapping.destination_config // []) |
      guard_norm_tf_mapping_destination),
    filter_patterns: (
      if (($mapping.filter_criteria // []) | length) == 0 then []
      else (($mapping.filter_criteria[0].filter // []) | map(.pattern // "") | sort)
      end),
    scaling_maximum_concurrency: (
      if (($mapping.scaling_config // []) | length) == 0 then 0
      else ($mapping.scaling_config[0].maximum_concurrency // 0)
      end),
    metrics: (
      if (($mapping.metrics_config // []) | length) == 0 then []
      else (($mapping.metrics_config[0].metrics // []) | sort)
      end),
    provisioned_poller: (
      if (($mapping.provisioned_poller_config // []) | length) == 0 then
        {minimum: 0, maximum: 0}
      else ($mapping.provisioned_poller_config[0] | {
        minimum: (.minimum_pollers // 0), maximum: (.maximum_pollers // 0)
      })
      end),
    source_access_configurations: (($mapping.source_access_configuration // []) |
      map({type: (.type // ""), uri: (.uri // "")}) | guard_sort_objects),
    non_sqs_source_config_count: ([
      ($mapping.amazon_managed_kafka_event_source_config // []),
      ($mapping.document_db_event_source_config // []),
      ($mapping.self_managed_event_source // []),
      ($mapping.self_managed_kafka_event_source_config // [])
    ] | map(length) | add),
    queues: (($mapping.queues // []) | sort),
    topics: (($mapping.topics // []) | sort),
    tags: (($mapping.tags_all // {}) + ($mapping.tags // {}))
  };
