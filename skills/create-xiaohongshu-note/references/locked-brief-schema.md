# Data Schemas

## LockedBrief

Required JSON fields:

```json
{
  "brand": "品牌或账号名",
  "product_or_service": "产品/服务/主题",
  "audience": "目标用户",
  "objective": "本篇笔记目标",
  "selected_topic": "已确认选题",
  "reference_sources": ["参考文件、链接、竞品、用户提供资料"],
  "reference_links": ["最终包需要展示的参考链接或记录 ID"],
  "formula_decomposition": {
    "source": "用户链接或飞书 learning 记录",
    "learning_schema_used": "采用的 learning 字段/结构",
    "selected_formula": "本轮拆解后确认采用的公式",
    "adaptation_notes": "如何适配当前客户/项目"
  },
  "must_include": ["必须包含的信息"],
  "must_avoid": ["必须避开的表达或风险"],
  "tone": "语气风格",
  "cover_spec": {
    "ratio": "3:4",
    "size": "1080x1440",
    "cover_count": 1,
    "inner_pages": 0,
    "carousel_requested": false,
    "visual_direction": "视觉方向",
    "customer_assets": []
  },
  "copy_spec": {
    "title_count": 5,
    "title_max_chars": 20,
    "body_length": "medium",
    "tag_count": 8,
    "first_comment": true
  },
  "delivery": {
    "output_dir": "",
    "docx": true,
    "feishu_bitable": true
  }
}
```

## XhsNotePack

Required JSON fields:

```json
{
  "cover": {
    "hook": "封面主标题",
    "subtitle": "封面副标题",
    "prompt": "最终生图 prompt",
    "asset_usage_plan": [],
    "final_image_path": ""
  },
  "cover_design": {
    "reference_cover_analysis": [],
    "cover_text": {
      "headline": "封面主标题",
      "subline": "封面副标题",
      "badge_or_proof": "封面角标/证据点，不能写CTA"
    },
    "layout_instructions": "封面排版、字体、色彩、主视觉说明",
    "style_instructions": "设计风格和差异化要求",
    "image_prompt": "最终封面生成或设计提示词",
    "final_cover_status": "produced | blocked | not_yet_generated",
    "final_cover_path": ""
  },
  "carousel": {
    "requested": false,
    "page_count": 0,
    "reason": "",
    "status": "not_requested | produced | blocked | not_yet_generated"
  },
  "inner_pages": [
    {
      "page": 1,
      "purpose": "",
      "text": "",
      "layout_instructions": "",
      "image_prompt": "",
      "status": "produced | blocked | not_yet_generated",
      "image_path": ""
    }
  ],
  "formula_decomposition": {
    "references": [],
    "learning_schema_used": "",
    "selected_formula": "",
    "application_notes": ""
  },
  "titles": ["标题候选1", "标题候选2"],
  "selected_title": "推荐标题",
  "body": "笔记正文",
  "tags": ["#标签"],
  "first_comment": "评论区首评",
  "audit_check": {
    "summary": "通过/需修改",
    "plan_fit": "",
    "formula_origin": "",
    "title_length_check": "",
    "native_copy_check": "",
    "final_cover_produced": false,
    "inner_pages_produced": false,
    "carousel_requested": false,
    "reference_similarity_under_50_percent": "",
    "risks": [],
    "required_fixes": []
  },
  "asset_paths": [],
  "image_generation_status": {
    "status": "pending",
    "model": "gpt-image-2-pro",
    "fallback_model": "image-2",
    "prompt_hash": ""
  }
}
```

## DeliveryManifest

Include:

- `brief_path`
- `note_pack_path`
- `docx_path`
- `wps_project_path`
- `cover_paths`
- `feishu_base_token`
- `feishu_table_id`
- `feishu_record_id`
- `created_at`
- `warnings`
