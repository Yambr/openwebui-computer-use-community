#!/usr/bin/env node
/**
 * Extract prompts from Claude Code cli.js
 */

const fs = require('fs');
const path = require('path');

const CLI_PATH = process.env.HOME + '/.nvm/versions/node/v23.3.0/lib/node_modules/@anthropic-ai/claude-code/cli.js';
const OUTPUT_DIR = path.join(__dirname, 'prompts');

// Read cli.js
const cliContent = fs.readFileSync(CLI_PATH, 'utf-8');

// Helper to find and extract template literal content
function extractBetween(content, startMarker, endMarker, offset = 0) {
    const startIdx = content.indexOf(startMarker, offset);
    if (startIdx === -1) return null;
    const endIdx = content.indexOf(endMarker, startIdx + startMarker.length);
    if (endIdx === -1) return null;
    return {
        text: content.slice(startIdx + startMarker.length, endIdx),
        endIndex: endIdx
    };
}

// Extract prompts by searching for known patterns
const prompts = {};

// 1. Bash Agent
const bashStart = cliContent.indexOf('You are a command execution specialist for Claude Code');
if (bashStart !== -1) {
    const bashEnd = cliContent.indexOf('Complete the requested operations efficiently.`', bashStart);
    if (bashEnd !== -1) {
        prompts['bash'] = cliContent.slice(bashStart, bashEnd + 'Complete the requested operations efficiently.'.length);
    }
}

// 2. General-purpose Agent
const gpStart = cliContent.indexOf("You are an agent for Claude Code, Anthropic's official CLI for Claude. Given the user's message");
if (gpStart !== -1) {
    const gpEnd = cliContent.indexOf('For clear communication, avoid using emojis.`}});', gpStart);
    if (gpEnd !== -1) {
        prompts['general-purpose'] = cliContent.slice(gpStart, gpEnd + 'For clear communication, avoid using emojis.'.length);
    }
}

// 3. Explore Agent
const exploreStart = cliContent.indexOf('You are a file search specialist for Claude Code');
if (exploreStart !== -1) {
    const exploreEnd = cliContent.indexOf('Complete the user\'s search request efficiently and report your findings clearly.`', exploreStart);
    if (exploreEnd !== -1) {
        prompts['explore'] = cliContent.slice(exploreStart, exploreEnd + "Complete the user's search request efficiently and report your findings clearly.".length);
    }
}

// 4. Plan Agent
const planStart = cliContent.indexOf('You are a software architect and planning specialist for Claude Code');
if (planStart !== -1) {
    const planEnd = cliContent.indexOf('You do NOT have access to file editing tools.`', planStart);
    if (planEnd !== -1) {
        prompts['plan'] = cliContent.slice(planStart, planEnd + 'You do NOT have access to file editing tools.'.length);
    }
}

// 5. Statusline-setup Agent
const statusStart = cliContent.indexOf('You are a status line setup agent for Claude Code');
if (statusStart !== -1) {
    const statusEnd = cliContent.indexOf('Also ensure that the user is informed that they can ask Claude to continue to make changes to the status line.\n`}}', statusStart);
    if (statusEnd !== -1) {
        prompts['statusline-setup'] = cliContent.slice(statusStart, statusEnd + 'Also ensure that the user is informed that they can ask Claude to continue to make changes to the status line.'.length);
    }
}

// 6. Claude-code-guide Agent
const guideStart = cliContent.indexOf('You are the Claude guide agent. Your primary responsibility');
if (guideStart !== -1) {
    const guideEnd = cliContent.indexOf('Complete the user\'s request by providing accurate, documentation-based guidance.`', guideStart);
    if (guideEnd !== -1) {
        prompts['claude-code-guide'] = cliContent.slice(guideStart, guideEnd + "Complete the user's request by providing accurate, documentation-based guidance.".length);
    }
}

// 7. Explanatory Mode
const explStart = cliContent.indexOf('You are an interactive CLI tool that helps users with software engineering tasks. In addition to software engineering tasks, you should provide educational insights');
if (explStart !== -1) {
    const explEnd = cliContent.indexOf('# Explanatory Style Active', explStart);
    if (explEnd !== -1) {
        // Find the end of the whole prompt
        const fullEnd = cliContent.indexOf('${nwB}`}', explEnd);
        if (fullEnd !== -1) {
            prompts['mode-explanatory'] = cliContent.slice(explStart, fullEnd).replace(/\$\{nwB\}/g, '[INSIGHTS_PROMPT]');
        }
    }
}

// 8. Learning Mode
const learnStart = cliContent.indexOf('You are an interactive CLI tool that helps users with software engineering tasks. In addition to software engineering tasks, you should help users learn more about the codebase through hands-on practice');
if (learnStart !== -1) {
    const learnEnd = cliContent.indexOf('${nwB}`}}', learnStart);
    if (learnEnd !== -1) {
        prompts['mode-learning'] = cliContent.slice(learnStart, learnEnd).replace(/\$\{nwB\}/g, '[INSIGHTS_PROMPT]').replace(/\$\{A1\.bullet\}/g, '•');
    }
}

// 9. Code Review Skill
const reviewStart = cliContent.indexOf('You are an expert code reviewer. Follow these steps:');
if (reviewStart !== -1) {
    const reviewEnd = cliContent.indexOf('Format your review with clear sections and bullet points.', reviewStart);
    if (reviewEnd !== -1) {
        prompts['skill-code-review'] = cliContent.slice(reviewStart, reviewEnd + 'Format your review with clear sections and bullet points.'.length);
    }
}

// 10. Security Review Skill
const secStart = cliContent.indexOf('You are a senior security engineer conducting a focused security review');
if (secStart !== -1) {
    // Find a good ending
    const secEnd = cliContent.indexOf('SEVERITY GUIDELINES:', secStart);
    if (secEnd !== -1) {
        prompts['skill-security-review'] = cliContent.slice(secStart, secEnd + 'SEVERITY GUIDELINES:'.length) + '\n[... continues with severity guidelines ...]';
    }
}

// 11. Summary/Compact Prompt
const summaryStart = cliContent.indexOf('Your task is to create a detailed summary of the conversation so far, paying close attention to the user\'s explicit requests');
if (summaryStart !== -1) {
    const summaryEnd = cliContent.indexOf('</example>\n`;return', summaryStart);
    if (summaryEnd !== -1) {
        prompts['system-summary'] = cliContent.slice(summaryStart, summaryEnd + '</example>'.length);
    }
}

// Create output directory
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

// Write prompts to files
for (const [name, content] of Object.entries(prompts)) {
    const filePath = path.join(OUTPUT_DIR, `${name}.txt`);
    // Clean up escape sequences
    const cleaned = content
        .replace(/\\n/g, '\n')
        .replace(/\\t/g, '\t')
        .replace(/\\`/g, '`')
        .replace(/\\\\/g, '\\')
        .replace(/\$\{[^}]+\}/g, (match) => {
            // Replace template variables with readable names
            if (match.includes('K9')) return 'Bash';
            if (match.includes('gI')) return 'Glob';
            if (match.includes('BI')) return 'Grep';
            if (match.includes('C3')) return 'Read';
            if (match.includes('mI')) return 'WebFetch';
            if (match.includes('BR')) return 'WebSearch';
            if (match.includes('Q9.name')) return 'Bash';
            return match;
        });

    fs.writeFileSync(filePath, cleaned);
    console.log(`Written: ${filePath}`);
}

// 12. Tool descriptions - Bash (full)
const bashToolStart = cliContent.indexOf('Executes a given bash command in a persistent shell session');
if (bashToolStart !== -1) {
    const bashToolEnd = cliContent.indexOf('# Other common operations', bashToolStart);
    if (bashToolEnd !== -1) {
        prompts['tool-bash'] = cliContent.slice(bashToolStart, bashToolEnd);
    }
}

// 13. Read tool
const readToolStart = cliContent.indexOf('Reads a file from the local filesystem. You can access any file directly');
if (readToolStart !== -1) {
    const readToolEnd = cliContent.indexOf('If you read a file that exists but has empty contents', readToolStart);
    if (readToolEnd !== -1) {
        prompts['tool-read'] = cliContent.slice(readToolStart, readToolEnd + 'If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.'.length);
    }
}

// 14. Edit tool
const editToolStart = cliContent.indexOf('Performs exact string replacements in files');
if (editToolStart !== -1) {
    const editToolEnd = cliContent.indexOf('Use `replace_all` for replacing and renaming strings across the file', editToolStart);
    if (editToolEnd !== -1) {
        prompts['tool-edit'] = cliContent.slice(editToolStart, editToolEnd + 'Use `replace_all` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance.'.length);
    }
}

// 15. Write tool
const writeToolStart = cliContent.indexOf('Writes a file to the local filesystem.');
if (writeToolStart !== -1) {
    const writeToolEnd = cliContent.indexOf('Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.`', writeToolStart);
    if (writeToolEnd !== -1) {
        prompts['tool-write'] = cliContent.slice(writeToolStart, writeToolEnd + 'Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.'.length);
    }
}

// 16. Glob tool
const globToolStart = cliContent.indexOf('Fast file pattern matching tool that works with any codebase size');
if (globToolStart !== -1) {
    const globToolEnd = cliContent.indexOf('You can call multiple tools in a single response. It is always better to speculatively perform multiple searches', globToolStart);
    if (globToolEnd !== -1) {
        prompts['tool-glob'] = cliContent.slice(globToolStart, globToolEnd + 'You can call multiple tools in a single response. It is always better to speculatively perform multiple searches in parallel if they are potentially useful.'.length);
    }
}

// 17. Grep tool
const grepToolStart = cliContent.indexOf('A powerful search tool built on ripgrep');
if (grepToolStart !== -1) {
    const grepToolEnd = cliContent.indexOf('Multiline matching: By default patterns match within single lines only', grepToolStart);
    if (grepToolEnd !== -1) {
        prompts['tool-grep'] = cliContent.slice(grepToolStart, grepToolEnd + 'Multiline matching: By default patterns match within single lines only. For cross-line patterns like `struct \\\\{[\\\\s\\\\S]*?field`, use `multiline: true`'.length);
    }
}

// 18. Task tool (subagent launcher)
const taskToolStart = cliContent.indexOf('Launch a new agent to handle complex, multi-step tasks autonomously');
if (taskToolStart !== -1) {
    const taskToolEnd = cliContent.indexOf('assistant: Uses the Task tool to launch the test-runner agent', taskToolStart);
    if (taskToolEnd !== -1) {
        prompts['tool-task'] = cliContent.slice(taskToolStart, taskToolEnd + 'assistant: Uses the Task tool to launch the test-runner agent\n</example>'.length);
    }
}

// 19. TodoWrite tool
const todoToolStart = cliContent.indexOf('Use this tool when you need to ask the user questions during execution');
if (todoToolStart !== -1) {
    // Find TodoWrite specifically
    const todoWriteStart = cliContent.indexOf('Use this tool to create and manage a structured task list', todoToolStart);
    if (todoWriteStart !== -1) {
        const todoWriteEnd = cliContent.indexOf('When in doubt, use this tool. Being proactive with task management', todoWriteStart);
        if (todoWriteEnd !== -1) {
            prompts['tool-todowrite'] = cliContent.slice(todoWriteStart, todoWriteEnd + 'When in doubt, use this tool. Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.'.length);
        }
    }
}

// Write additional prompts
for (const [name, content] of Object.entries(prompts)) {
    if (name.startsWith('tool-')) {
        const filePath = path.join(OUTPUT_DIR, `${name}.txt`);
        if (!fs.existsSync(filePath)) {
            const cleaned = content
                .replace(/\\n/g, '\n')
                .replace(/\\t/g, '\t')
                .replace(/\\`/g, '`')
                .replace(/\\\\/g, '\\')
                .replace(/\$\{[^}]+\}/g, (match) => {
                    if (match.includes('K9')) return 'Bash';
                    if (match.includes('gI')) return 'Glob';
                    if (match.includes('BI')) return 'Grep';
                    if (match.includes('C3')) return 'Read';
                    if (match.includes('f3')) return 'Edit';
                    if (match.includes('eZ')) return 'Write';
                    if (match.includes('mI')) return 'WebFetch';
                    if (match.includes('BR')) return 'WebSearch';
                    if (match.includes('mW.name')) return 'TodoWrite';
                    if (match.includes('b3')) return 'Task';
                    if (match.includes('pZ1')) return '600000';
                    if (match.includes('TKA')) return '120000';
                    if (match.includes('LyA')) return '30000';
                    return match;
                });
            fs.writeFileSync(filePath, cleaned);
            console.log(`Written: ${filePath}`);
        }
    }
}

console.log(`\nExtracted ${Object.keys(prompts).length} prompts to ${OUTPUT_DIR}`);
