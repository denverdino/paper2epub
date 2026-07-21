local eq_labels = {}
local numbering_scope = "global"
local chapter_count = 0
local chapter_prefix = nil
local counters = {equation = 0, figure = 0, table = 0}
local subequations_by_label = {}
local numbered_labels = {}

local function remember_label(identifier, number, prefix)
    if identifier and identifier ~= "" then
        numbered_labels[identifier] = {
            number = tostring(number),
            prefix = prefix,
        }
    end
end

local function numbered_heading(inlines)
    for _, inline in ipairs(inlines) do
        if inline.t == "Strong" then
            local heading = pandoc.utils.stringify(inline)
            local prefix, number = heading:match("^(.-)%s+(%d+)[%p%s]*$")
            if number then return prefix, number end
        end
    end
    return nil, nil
end

local function collect_numbered_paragraph(el)
    local prefix, number = numbered_heading(el.content)
    if not number then return end
    for _, inline in ipairs(el.content) do
        if inline.t == "Span" then
            remember_label(inline.identifier, number, prefix)
        end
    end
end

local function collect_numbered_div(el)
    local pending_identifiers = {}
    for _, block in ipairs(el.content) do
        if block.t == "Div" and block.identifier ~= "" then
            table.insert(pending_identifiers, block.identifier)
        elseif block.t == "Para" then
            local prefix, number = numbered_heading(block.content)
            if number then
                for _, identifier in ipairs(pending_identifiers) do
                    remember_label(identifier, number, prefix)
                end
                pending_identifiers = {}
            end
        end
    end
end

local function collect_explicit_appendix_header(el)
    local text = pandoc.utils.stringify(el.content)
    local number = text:match("^([A-Z]+[%.%d]*)%s+")
    if number then
        remember_label(el.identifier, number, "Section")
    end
end

local AUTOREF_LOWER = {
    fig = "Fig.\u{00a0}",
    tab = "Table\u{00a0}",
    sec = "\u{00a7}",
    eq = "Eq.\u{00a0}",
}

local AUTOREF_UPPER = {
    fig = "Figure\u{00a0}",
    tab = "Table\u{00a0}",
    sec = "Section\u{00a0}",
    eq = "Equation\u{00a0}",
}

local function ref_kind(label)
    if label:match("^fig:") then return "fig" end
    if label:match("^tab:") then return "tab" end
    if label:match("^sec:") then return "sec" end
    if label:match("^eq:") then return "eq" end
    return nil
end

local NUMBERED_EQUATION_ENVIRONMENTS = {
    align = true,
    equation = true,
    eqnarray = true,
    gather = true,
    multline = true,
}

local function has_class(el, class_name)
    for _, class in ipairs(el.classes) do
        if class == class_name then return true end
    end
    return false
end

local function reset_counters()
    for kind, _ in pairs(counters) do counters[kind] = 0 end
end

local function collect_chapter(el)
    if numbering_scope ~= "chapter" or el.level ~= 1 then return end
    if has_class(el, "unnumbered") then
        local explicit = pandoc.utils.stringify(el.content):match("^([A-Z]+)%s+")
        if explicit then
            chapter_prefix = explicit
            reset_counters()
        end
        return
    end
    chapter_count = chapter_count + 1
    chapter_prefix = tostring(chapter_count)
    reset_counters()
end

local function next_number(kind)
    counters[kind] = counters[kind] + 1
    if numbering_scope == "chapter" and chapter_prefix then
        return chapter_prefix .. "." .. counters[kind]
    end
    return tostring(counters[kind])
end

local function split_plain(value, separator)
    local parts = {}
    local start = 1
    while true do
        local stop = value:find(separator, start, true)
        if not stop then
            table.insert(parts, value:sub(start))
            return parts
        end
        table.insert(parts, value:sub(start, stop - 1))
        start = stop + #separator
    end
end

local function parse_subequations(value)
    if value == "" then return end
    for _, encoded_group in ipairs(split_plain(value, ";")) do
        local fields = split_plain(encoded_group, "|")
        local group = {parent = fields[1], children = {}}
        for index = 2, #fields do
            local label = fields[index]
            table.insert(group.children, label)
            subequations_by_label[label] = group
        end
    end
end

local function alphabetic_suffix(index)
    local suffix = ""
    while index > 0 do
        local remainder = (index - 1) % 26
        suffix = string.char(string.byte("a") + remainder) .. suffix
        index = math.floor((index - 1) / 26)
    end
    return suffix
end

local function equation_anchor(identifier)
    return pandoc.Span({}, pandoc.Attr(identifier, {"equation-anchor"}))
end

local function collect_equation(el)
    if el.mathtype ~= "DisplayMath" then return nil end
    local environment = el.text:match("\\begin%s*%{%s*([%a@]+)%s*%}")
    if not NUMBERED_EQUATION_ENVIRONMENTS[environment] then return nil end

    local labels = {}
    for label in el.text:gmatch("\\label%s*%{([^}]+)%}") do
        table.insert(labels, label)
    end
    local numbers = {}
    local anchors = {}
    local identifier = labels[1] or ""
    local subequations = labels[1] and subequations_by_label[labels[1]] or nil
    if subequations then
        local parent_number = next_number("equation")
        if subequations.parent ~= "" then
            identifier = subequations.parent
            eq_labels[subequations.parent] = parent_number
            remember_label(subequations.parent, parent_number, "Equation")
        end
        for index, label in ipairs(subequations.children) do
            local number = parent_number .. alphabetic_suffix(index)
            table.insert(numbers, number)
            eq_labels[label] = number
            remember_label(label, number, "Equation")
            if label ~= identifier then
                table.insert(anchors, equation_anchor(label))
            end
        end
    else
        local number_count = math.max(1, #labels)
        for index = 1, number_count do
            local number = next_number("equation")
            table.insert(numbers, number)
            local label = labels[index]
            if label then
                eq_labels[label] = number
                remember_label(label, number, "Equation")
                if index > 1 then
                    table.insert(anchors, equation_anchor(label))
                end
            end
        end
    end

    local number_text = "(" .. table.concat(numbers, ") (") .. ")"
    table.insert(anchors, el)
    table.insert(anchors, pandoc.Span({pandoc.Str(number_text)}, pandoc.Attr(
        "", {"equation-number"}
    )))
    local rendered = pandoc.Span(
        anchors,
        pandoc.Attr(identifier, {"numbered-equation"})
    )
    return rendered, false
end

local function prefix_caption(el, prefix, number)
    if el.caption and el.caption.long and #el.caption.long > 0 then
        local block = el.caption.long[1]
        if block.content then
            block.content:insert(1, pandoc.Space())
            block.content:insert(1, pandoc.Strong{
                pandoc.Str(prefix .. " " .. number .. ":")
            })
        end
    end
end

local function number_figure(el)
    local number = next_number("figure")
    remember_label(el.identifier, number, "Figure")
    local aliases = {}
    el:walk({
        Span = function(span)
            if span.identifier ~= ""
                and span.attributes["label"] == span.identifier then
                table.insert(aliases, span.identifier)
            end
        end,
    })
    for index, identifier in ipairs(aliases) do
        remember_label(
            identifier,
            number .. alphabetic_suffix(index),
            "Figure"
        )
    end
    prefix_caption(el, "Figure", number)
    return el
end

local function number_table(el)
    local number = next_number("table")
    remember_label(el.identifier, number, "Table")
    prefix_caption(el, "Table", number)
    return el
end

return {
    -- Pass 1: read Python's source-aware numbering decision.
    {
        Meta = function(meta)
            local configured = meta["paper2epub-numbering-scope"]
            if configured then
                numbering_scope = pandoc.utils.stringify(configured)
            end
            local subequations = meta["paper2epub-subequations"]
            if subequations then
                parse_subequations(pandoc.utils.stringify(subequations))
            end
        end,
    },

    -- Pass 2: assign every numbered object once, in document order.
    {
        Pandoc = function(doc)
            return doc:walk({
                traverse = "topdown",
                Header = collect_chapter,
                Math = collect_equation,
                Figure = number_figure,
                Table = number_table,
            })
        end,
    },

    -- Pass 3: collect generated theorem, algorithm, and appendix labels.
    {
        Para = collect_numbered_paragraph,
        Div = collect_numbered_div,
        Header = collect_explicit_appendix_header,
    },

    -- Pass 4: modify remaining elements and resolve references.
    {
        Image = function(el)
            el.src = el.src:gsub("%.pdf$", ".png")
            return el
        end,

        Link = function(el)
            local rtype = el.attributes["reference-type"]
            if not rtype then return nil end
            local ref = el.attributes["reference"] or ""
            local unresolved = pandoc.utils.stringify(el.content) == "[" .. ref .. "]"

            if rtype == "eqref" then
                local num = eq_labels[ref]
                if num then
                    el.content = {pandoc.Str("(" .. num .. ")")}
                end
                return el
            end

            if rtype == "ref" then
                local target = numbered_labels[ref]
                if target then
                    el.content = {pandoc.Str(target.number)}
                end
                return el
            end

            if rtype == "ref+label" or rtype == "ref+Label" then
                local kind = ref_kind(ref)
                local target = numbered_labels[ref]
                if target then
                    el.content = {pandoc.Str(target.number)}
                end
                if kind then
                    local map = rtype == "ref+Label" and AUTOREF_UPPER
                                                      or AUTOREF_LOWER
                    local prefix = map[kind]
                    if prefix then
                        table.insert(el.content, 1, pandoc.Str(prefix))
                    end
                elseif unresolved and target and target.prefix then
                    table.insert(
                        el.content, 1,
                        pandoc.Str(target.prefix .. "\u{00a0}")
                    )
                end
                -- Fix unresolved refs: pandoc renders them as [label]
                local txt = pandoc.utils.stringify(el.content)
                local escaped = ref:gsub("([%-%.%+])", "%%%1")
                if txt:match("%[" .. escaped .. "%]") then
                    el.content = {pandoc.Str(txt:gsub("%[" .. escaped .. "%]", "link"))}
                end
                return el
            end
        end,
    },
}
