/*
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

import { App } from "aws-cdk-lib";
import * as init from "../lib/pipeline/common/opensearch-stack";
describe("OpenSearch Admin Stack", () => {
  test("OpenSearch Admin Stack", () => {
    const app = new App();
    // WHEN
    const stack = new init.OpenSearchAdminStack(app, "OpenSearchAdminStack");
  });
});
