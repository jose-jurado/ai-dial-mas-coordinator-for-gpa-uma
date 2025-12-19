from copy import deepcopy
import json
from typing import Any

from aidial_client import AsyncDial
from aidial_sdk.chat_completion import Role, Choice, Request, Message, Stage
from pydantic import StrictStr

from task.coordination.gpa import GPAGateway
from task.coordination.ums_agent import UMSAgentGateway
from task.logging_config import get_logger
from task.models import CoordinationRequest, AgentName
from task.prompts import COORDINATION_REQUEST_SYSTEM_PROMPT, FINAL_RESPONSE_SYSTEM_PROMPT
from task.stage_util import StageProcessor

logger = get_logger(__name__)


class MASCoordinator:

    def __init__(self, endpoint: str, deployment_name: str, ums_agent_endpoint: str):
        self.endpoint = endpoint
        self.deployment_name = deployment_name
        self.ums_agent_endpoint = ums_agent_endpoint

    async def handle_request(self, choice: Choice, request: Request) -> Message:
        #TODO:
        # 1. Create AsyncDial client (api_version='2025-01-01-preview')
        # 2. Open stage for Coordination Request (StageProcessor will help with that)
        # 3. Prepare coordination request
        # 4. Add to the stage generated coordination request and close the stage
        # 5. Handle coordination request (don't forget that all the content that will write called agent need to provide to stage)
        # 6. Generate final response based on the message from called agent
        client = AsyncDial(base_url=self.endpoint, api_version='2025-01-01-preview', api_key=request.api_key)

        coordination_stage = StageProcessor.open_stage(choice=choice, stage_name="Coordination Request")
        coordination_request = await self.__prepare_coordination_request(client=client, request=request)
        coordination_stage.append_content(f"```json\n{coordination_request.model_dump_json(indent=2)}\n```\n")
        StageProcessor.close_stage(stage=coordination_stage)

        handle_stage = StageProcessor.open_stage(choice=choice, stage_name="Handling Request")
        agent_message = await self.__handle_coordination_request(
            coordination_request=coordination_request,
            choice=choice,
            stage=handle_stage,
            request=request
        )
        StageProcessor.close_stage(stage=handle_stage)

        final_response = await self.__final_response(
            client=client,
            choice=choice,
            request=request,
            agent_message=agent_message
        )
        logger.info("Final response generated: " + final_response.model_dump_json())
        return final_response

    async def __prepare_coordination_request(self, client: AsyncDial, request: Request) -> CoordinationRequest:
        response = await client.chat.completions.create(
            messages=self.__prepare_messages(request, COORDINATION_REQUEST_SYSTEM_PROMPT),
            deployment_name=self.deployment_name,
            extra_body={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": CoordinationRequest.model_json_schema()
                    }
                },
            }
        )
        
        resp_content = json.loads(response.choices[0].message.content)
        return CoordinationRequest.model_validate(resp_content)

    def __prepare_messages(self, request: Request, system_prompt: str) -> list[dict[str, Any]]:
        messages = [{
            "role": Role.SYSTEM,
            "content": system_prompt
        }]
        for message in request.messages:
            if message.role == Role.USER and message.custom_content is not None:
                msg_copy = deepcopy(message)
                messages.append({
                    "role": Role.USER,
                    "content": StrictStr(msg_copy.content)
                })
            else:
                messages.append(message.model_dump(exclude_none=True))
        return messages

    async def __handle_coordination_request(
            self,
            coordination_request: CoordinationRequest,
            choice: Choice,
            stage: Stage,
            request: Request
    ) -> Message:
        if coordination_request.agent_name is AgentName.GPA:
            return await GPAGateway(endpoint=self.endpoint).response(
                choice=choice,
                request=request,
                stage=stage,
                additional_instructions=coordination_request.additional_instructions,
            )

        elif coordination_request.agent_name is AgentName.UMS:
            return await UMSAgentGateway(ums_agent_endpoint=self.ums_agent_endpoint).response(
                choice=choice,
                request=request,
                stage=stage,
                additional_instructions=coordination_request.additional_instructions,
            )
        else:
            raise ValueError(f"Unsupported Agent: {coordination_request.agent_name}")

    async def __final_response(
            self, client: AsyncDial,
            choice: Choice,
            request: Request,
            agent_message: Message
    ) -> Message:
        #TODO:
        # 1. Prepare messages with FINAL_RESPONSE_SYSTEM_PROMPT
        # 2. Make augmentation of retrieved agent response (as context) with user request (as user request)
        # 3. Update last message content with augmented prompt
        # 4. Call LLM with streaming
        # 5. Stream final response to choice
    
        messages = self.__prepare_messages(request, FINAL_RESPONSE_SYSTEM_PROMPT)
        augmented_request = f"## CONTEXT:\n {agent_message.content}\n ---\n ## USER_REQUEST: \n {messages[-1]["content"]}"
        messages[-1]["content"] = augmented_request

        chunks = await client.chat.completions.create(
            stream=True,
            messages=messages,
            deployment_name=self.deployment_name
        )

        content = ''
        async for chunk in chunks:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    choice.append_content(delta.content)
                    content += delta.content

        return Message(
            role=Role.ASSISTANT,
            content=StrictStr(content),
            custom_content=agent_message.custom_content
        )
