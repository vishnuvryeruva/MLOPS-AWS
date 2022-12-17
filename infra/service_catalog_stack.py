from pathlib import Path

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    CfnParameter,
    Duration,
    Stack,
    Tags,
    CustomResource,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_servicecatalog_alpha as servicecatalog
from constructs import Construct

from infra.mlops_featurestore_construct import MlopsFeaturestoreStack
from infra.utils import code_asset_upload, generate_template, snake2pascal


class ServiceCatalogStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        products_launch_role_arn = self.node.try_get_context("MlopsDemo:LaunchRole")
        if products_launch_role_arn is None:
            products_launch_role_arn = self.format_arn(
                region="",
                service="iam",
                resource="role",
                resource_name="service-role/AmazonSageMakerServiceCatalogProductsLaunchRole",
            )
        products_use_role_arn = self.node.try_get_context("MlopsDemo:UseRole")
        if products_use_role_arn is None:
            products_use_role_arn = self.format_arn(
                region="",
                service="iam",
                resource="role",
                resource_name="service-role/AmazonSageMakerServiceCatalogProductsUseRole",
            )

        # Define CloudFormation Parameters
        portfolio_name = CfnParameter(
            self,
            "PortfolioName",
            type="String",
            description="The name of the portfolio",
            default="SageMaker Organization Templates",
            min_length=1,
        )
        portfolio_owner = CfnParameter(
            self,
            "PortfolioOwner",
            type="String",
            description="The owner of the portfolio",
            default="Administrator",
            min_length=1,
            max_length=50,
        )
        product_version = CfnParameter(
            self,
            "ProductVersion",
            type="String",
            description="The product version to deploy",
            default="1.0",
            min_length=1,
        )
        studio_user_role_arn = CfnParameter(
            self,
            "StudioUserRoleARN",
            type="String",
            description="Studio User Role ARN",
            min_length=1,
            allowed_pattern="^arn:aws[a-z\\-]*:iam::\\d{12}:role/?[a-zA-Z_0-9+=,.@\\-_/]+$",
            default=self.node.try_get_context(key="demouserrole"),
        )

        products_launch_role = iam.Role.from_role_arn(
            self,
            "LaunchRole",
            products_launch_role_arn,
        )
        products_use_role = iam.Role.from_role_arn(
            self,
            "ProductsUseRole",
            products_use_role_arn,
        )

        code_assets = {
            f"{snake2pascal(k.name)}": dict(
                s3_object_key=code_asset_upload(
                    self, k, products_launch_role
                ).s3_object_key,
            )
            for k in Path("repos").glob("*")
            if k.is_dir()
        }

        demo_asset_o = code_asset_upload(
            self, Path("demo-workspace"), products_launch_role
        )
        demo_asset = dict(s3_object_key=demo_asset_o.s3_object_key)

        CfnOutput(
            self,
            "SeedBucketNameOutput",
            value=demo_asset_o.s3_bucket_name,
            export_name="MLOpsDemo-SeedBucketName-f5e74ee2",
        )

        product_template = generate_template(
            MlopsFeaturestoreStack,
            "MLOpsCfnStack",
            code_assets=code_assets,
            demo_asset=demo_asset,
        )

        portfolio = servicecatalog.Portfolio(
            self,
            "Portfolio",
            display_name=portfolio_name.value_as_string,
            provider_name=portfolio_owner.value_as_string,
            description="Organization templates for MLOps Demo",
        )

        product = servicecatalog.CloudFormationProduct(
            self,
            "Product",
            owner=portfolio_owner.value_as_string,
            product_name="Amazon SageMaker MLOps Demo",
            product_versions=[
                servicecatalog.CloudFormationProductVersion(
                    cloud_formation_template=servicecatalog.CloudFormationTemplate.from_asset(
                        product_template
                    ),
                    product_version_name=product_version.value_as_string,
                )
            ],
            description="Amazon SageMaker MLOps demo project with "
            "Feature Ingestion, "
            "Model Build, "
            "and Deployment pipelines",
        )
        Tags.of(product).add(key="sagemaker:studio-visibility", value="true")

        portfolio.add_product(product)
        studio_user_role = iam.Role.from_role_arn(
            self,
            "execution_role_arn",
            role_arn=studio_user_role_arn.value_as_string,
        )

        # Lambda Fn to download the seed code for all the repos and the product template from the GitHub repository
        powertools_lambda_layer = lambda_.LayerVersion.from_layer_version_arn(
            self,
            "AwsLambdaPowerToolsLayer",
            layer_version_arn=self.format_arn(
                account="017000801446",
                service="lambda",
                resource="layer",
                resource_name="AWSLambdaPowertoolsPython:4",
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            ),
        )
        with Path("lambdas/functions/role_name/lambda_main.py").open() as f:
            seed_lambda_code = f.read()

        role_split_lambda = lambda_.Function(
            self,
            "RoleSplitLambda",
            code=lambda_.Code.from_inline(seed_lambda_code),
            handler="index.lambda_handler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            timeout=Duration.seconds(100),
            layers=[
                powertools_lambda_layer,
            ],
        )
        cr = CustomResource(
            self,
            f"CrRoleSplitLambda",
            service_token=role_split_lambda.function_arn,
            properties=dict(
                RoleArn=studio_user_role_arn.value_as_string,
            ),
        )

        CfnOutput(
            self,
            "RoleNameOutput",
            value=cr.get_att_string("RoleName"),
            export_name="MLOpsDemo-RoleName-f5e74ee2",
        )
        CfnOutput(
            self,
            "RoleNameArn",
            value=studio_user_role_arn.value_as_string,
            export_name="MLOpsDemo-RoleArn-f5e74ee2",
        )

        products_launch_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["iam:PutRolePolicy", "iam:DeleteRolePolicy"],
                resources=[
                    studio_user_role_arn.value_as_string,
                    studio_user_role.role_arn,
                ],
            )
        )

        portfolio.give_access_to_role(studio_user_role)
        portfolio.set_launch_role(product, products_launch_role)
        launch_role_policies(products_launch_role, self)
        products_launch_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:PutRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:getRolePolicy",
                ],
                resources=[products_use_role.role_arn],
            )
        )


def launch_role_policies(target_role: iam.Role, stack: Stack):
    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=[
                "SNS:CreateTopic",
                "SNS:GetTopicAttributes",
                "SNS:DeleteTopic",
                "SNS:ListTagsForResource",
                "SNS:TagResource",
                "SNS:UnTagResource",
                "SNS:Subscribe",
            ],
            resources=[
                stack.format_arn(
                    service="sns",
                    resource="sagemaker-*",
                )
            ],
        )
    )
    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=[
                "SNS:Unsubscribe",
            ],
            resources=["*"],
        )
    )
    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=["codebuild:BatchGetProjects"],
            resources=[
                stack.format_arn(
                    service="codebuild",
                    resource="project",
                    resource_name="sagemaker*",
                ),
            ],
        )
    )
    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=["s3:*"],
            resources=[
                stack.format_arn(
                    service="s3",
                    region="",
                    account="",
                    resource="cdktoolkit-stagingbucket-*",
                ),
            ],
        )
    )

    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name="cdk-bootstrap/*",
                ),
            ],
        )
    )

    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=[
                "ssm:PutParameter",
                "ssm:DeleteParameter",
                "ssm:AddTagsToResource",
                "ssm:LabelParameterVersion",
                "ssm:ListTagsForResource",
                "ssm:RemoveTagsFromResource",
                "ssm:DeleteParameter",
                "ssm:DeleteParameters",
            ],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name="sagemaker*",
                ),
            ],
        )
    )
    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=[
                "ssm:DescribeParameters",
            ],
            resources=["*"],
        )
    )

    target_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=["lambda:GetLayerVersion"],
            resources=[
                stack.format_arn(
                    service="lambda",
                    account="017000801446",
                    resource="layer",
                    resource_name="AWSLambdaPowertoolsPython:4",
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                ),
            ],
        )
    )
