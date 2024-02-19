# Authorization with Lambda@Edge

This solution will use Lambda@Edge and Cognito User Pools to authorize calls made to CloudFront and static content.

Solution overview:  

![Image showing the overview.](images/overview.png)

## How to deploy

This solution show how to setup everything in a different region than us-east-1.

To deploy the solution you need to do the following steps in order, deployment rely on [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)

* Deploy the Cloudformation template /eu-north-1/UserPool/template.yaml
* Deploy the Cloudformation template /us-east-1/EdgeLambda/template.yaml
* Deploy the Cloudformation template /eu-north-1/CloudFrontDistribution/template.yaml
